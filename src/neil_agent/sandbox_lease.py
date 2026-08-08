"""Bounded lifetime handle leases for Windows Sandbox host inputs and output."""

from __future__ import annotations

import ctypes
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Protocol, Self

from .errors import SandboxError

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x0001
_FILE_SHARE_WRITE = 0x0002
_OPEN_EXISTING = 3
_FILE_BEGIN = 0
_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class _HandleIdentity:
    volume: int
    index: int
    attributes: int
    links: int
    size: int
    last_write: int

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & _FILE_ATTRIBUTE_DIRECTORY)

    @property
    def is_reparse(self) -> bool:
        return bool(self.attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


@dataclass(frozen=True, slots=True)
class _PortableIdentity:
    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int

    @property
    def is_directory(self) -> bool:
        return stat.S_ISDIR(self.mode)


class _LeaseApi(Protocol):
    def open_path(self, path: Path, *, share_mode: int, flags: int) -> int: ...

    def query(self, handle: int) -> _HandleIdentity: ...

    def read(self, handle: int, maximum_bytes: int) -> bytes: ...

    def close(self, handle: int) -> None: ...


class HandleLease(Protocol):
    """The executor-facing lifetime lease contract."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def validate(self) -> None: ...

    def read_file(self, relative_path: str, maximum_bytes: int) -> bytes: ...

    def close(self) -> None: ...


class HandleLeaseFactory(Protocol):
    def __call__(
        self,
        root: Path,
        *,
        writable_root: bool = False,
        expected_names: frozenset[str] | None = None,
        max_entries: int,
        max_total_bytes: int,
    ) -> HandleLease: ...


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTimeLow", ctypes.c_uint32),
        ("ftCreationTimeHigh", ctypes.c_uint32),
        ("ftLastAccessTimeLow", ctypes.c_uint32),
        ("ftLastAccessTimeHigh", ctypes.c_uint32),
        ("ftLastWriteTimeLow", ctypes.c_uint32),
        ("ftLastWriteTimeHigh", ctypes.c_uint32),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _CtypesLeaseApi:
    __slots__ = (
        "_close_handle",
        "_create_file",
        "_get_information",
        "_read_file",
        "_set_file_pointer",
    )

    def __init__(self) -> None:
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise OSError("Win32 APIs are unavailable")
        kernel32 = win_dll("kernel32", use_last_error=True)
        self._create_file = kernel32.CreateFileW
        self._create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self._create_file.restype = ctypes.c_void_p
        self._get_information = kernel32.GetFileInformationByHandle
        self._get_information.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self._get_information.restype = ctypes.c_int
        self._set_file_pointer = kernel32.SetFilePointerEx
        self._set_file_pointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int64,
            ctypes.POINTER(ctypes.c_int64),
            ctypes.c_uint32,
        ]
        self._set_file_pointer.restype = ctypes.c_int
        self._read_file = kernel32.ReadFile
        self._read_file.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self._read_file.restype = ctypes.c_int
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [ctypes.c_void_p]
        self._close_handle.restype = ctypes.c_int

    def open_path(self, path: Path, *, share_mode: int, flags: int) -> int:
        handle = self._create_file(
            str(path),
            _GENERIC_READ,
            share_mode,
            None,
            _OPEN_EXISTING,
            flags,
            None,
        )
        if handle in {None, _INVALID_HANDLE_VALUE}:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        return int(handle)

    def query(self, handle: int) -> _HandleIdentity:
        information = _ByHandleFileInformation()
        if not self._get_information(handle, ctypes.byref(information)):
            raise OSError(
                ctypes.get_last_error(),
                "GetFileInformationByHandle failed",
            )
        return _HandleIdentity(
            volume=int(information.dwVolumeSerialNumber),
            index=(int(information.nFileIndexHigh) << 32)
            | int(information.nFileIndexLow),
            attributes=int(information.dwFileAttributes),
            links=int(information.nNumberOfLinks),
            size=(int(information.nFileSizeHigh) << 32) | int(information.nFileSizeLow),
            last_write=(int(information.ftLastWriteTimeHigh) << 32)
            | int(information.ftLastWriteTimeLow),
        )

    def read(self, handle: int, maximum_bytes: int) -> bytes:
        position = ctypes.c_int64()
        if not self._set_file_pointer(handle, 0, ctypes.byref(position), _FILE_BEGIN):
            raise OSError(ctypes.get_last_error(), "SetFilePointerEx failed")
        chunks: list[bytes] = []
        total = 0
        while total <= maximum_bytes:
            request = min(_READ_CHUNK_BYTES, maximum_bytes + 1 - total)
            buffer = ctypes.create_string_buffer(request)
            received = ctypes.c_uint32()
            if not self._read_file(
                handle,
                buffer,
                request,
                ctypes.byref(received),
                None,
            ):
                raise OSError(ctypes.get_last_error(), "ReadFile failed")
            count = int(received.value)
            if count == 0:
                break
            chunks.append(buffer.raw[:count])
            total += count
        return b"".join(chunks)

    def close(self, handle: int) -> None:
        if not self._close_handle(handle):
            raise OSError(ctypes.get_last_error(), "CloseHandle failed")


@dataclass(slots=True)
class _LeaseEntry:
    relative_path: str
    path: Path
    handle: int
    identity: _HandleIdentity | _PortableIdentity


class BoundedTreeLease:
    """Hold bounded tree identities for one complete host execution.

    Windows opens every object without ``FILE_SHARE_WRITE`` or
    ``FILE_SHARE_DELETE``.  A writable-root lease is the sole exception: it
    admits exporter-created children while still denying replacement of the
    export root.  POSIX descriptors provide identity regression coverage for
    injected test runners, but production WSB execution is Windows-only.
    """

    __slots__ = (
        "_api",
        "_closed",
        "_entries",
        "_expected_names",
        "_max_entries",
        "_max_total_bytes",
        "_root",
        "_windows",
        "_writable_root",
    )

    def __init__(
        self,
        root: Path,
        *,
        writable_root: bool = False,
        expected_names: frozenset[str] | None = None,
        max_entries: int,
        max_total_bytes: int,
        _api: _LeaseApi | None = None,
    ) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("lease root must be an absolute Path")
        if type(writable_root) is not bool:
            raise ValueError("writable_root must be a boolean")
        if expected_names is not None and (
            not isinstance(expected_names, frozenset)
            or any(
                not name or name in {".", ".."} or Path(name).name != name
                for name in expected_names
            )
        ):
            raise ValueError("expected names must be bounded root file names")
        for label, value in (
            ("entry limit", max_entries),
            ("byte limit", max_total_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"lease {label} must be a positive integer")
        self._root = root
        self._writable_root = writable_root
        self._expected_names = expected_names
        self._max_entries = max_entries
        self._max_total_bytes = max_total_bytes
        self._windows = os.name == "nt" or _api is not None
        self._api = _CtypesLeaseApi() if self._windows and _api is None else _api
        self._entries: dict[str, _LeaseEntry] = {}
        self._closed = False
        self._acquire()

    def __enter__(self) -> Self:
        if self._closed:
            raise SandboxError("closed handle lease cannot be reused")
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self.close()

    def validate(self) -> None:
        if self._closed:
            raise SandboxError("handle lease is already closed")
        if self._writable_root:
            self._validate_one(self._entries["."])
            return
        current = self._discover_paths()
        if set(current) != set(self._entries):
            raise SandboxError("leased tree entries changed during execution")
        for relative_path, entry in self._entries.items():
            if current[relative_path] != entry.path:
                raise SandboxError("leased tree path changed during execution")
            self._validate_one(entry)

    def read_file(self, relative_path: str, maximum_bytes: int) -> bytes:
        if type(maximum_bytes) is not int or maximum_bytes <= 0:
            raise ValueError("read limit must be a positive integer")
        if self._closed or self._writable_root:
            raise SandboxError("lease does not expose sealed file reads")
        entry = self._entries.get(relative_path)
        if entry is None or entry.identity.is_directory:
            raise SandboxError("leased result file is unavailable")
        self._validate_one(entry)
        if entry.identity.size > maximum_bytes:
            raise SandboxError("leased result file exceeds its size limit")
        try:
            if self._windows:
                assert self._api is not None
                raw = self._api.read(entry.handle, maximum_bytes)
            else:
                os.lseek(entry.handle, 0, os.SEEK_SET)
                chunks: list[bytes] = []
                total = 0
                while total <= maximum_bytes:
                    chunk = os.read(
                        entry.handle,
                        min(_READ_CHUNK_BYTES, maximum_bytes + 1 - total),
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                raw = b"".join(chunks)
        except OSError as error:
            raise SandboxError("leased file read failed") from error
        if len(raw) != entry.identity.size or len(raw) > maximum_bytes:
            raise SandboxError("leased file changed or exceeded its read limit")
        self._validate_one(entry)
        return raw

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        first_error: OSError | None = None
        for entry in reversed(tuple(self._entries.values())):
            try:
                if self._windows:
                    assert self._api is not None
                    self._api.close(entry.handle)
                else:
                    os.close(entry.handle)
            except OSError as error:
                if first_error is None:
                    first_error = error
        self._entries.clear()
        if first_error is not None:
            raise SandboxError(
                "handle lease could not close every held identity"
            ) from (first_error)

    def _acquire(self) -> None:
        if _path_has_reparse_component(self._root):
            raise SandboxError("handle lease root contains a reparse point")
        paths = {".": self._root} if self._writable_root else self._discover_paths()
        try:
            for relative_path, path in paths.items():
                self._entries[relative_path] = self._open_entry(relative_path, path)
            if self._writable_root:
                if any(self._root.iterdir()):
                    raise SandboxError("writable export lease must start empty")
            else:
                self.validate()
        except BaseException:
            self.close()
            raise

    def _discover_paths(self) -> dict[str, Path]:
        paths = {".": self._root}
        pending = [(PurePosixPath("."), self._root)]
        total_bytes = 0
        while pending:
            relative_root, directory = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(
                        tuple(iterator),
                        key=lambda item: (item.name.casefold(), item.name),
                    )
            except OSError as error:
                raise SandboxError("leased tree enumeration failed") from error
            if relative_root == PurePosixPath(".") and self._expected_names is not None:
                if {entry.name for entry in entries} != set(self._expected_names):
                    raise SandboxError(
                        "leased tree does not match expected root entries"
                    )
            for directory_entry in entries:
                relative = (
                    PurePosixPath(directory_entry.name)
                    if relative_root == PurePosixPath(".")
                    else relative_root / directory_entry.name
                )
                relative_text = relative.as_posix()
                if len(paths) >= self._max_entries + 1:
                    raise SandboxError("handle lease exceeds its entry limit")
                path = Path(directory_entry.path)
                try:
                    # CPython's Windows DirEntry cache can omit file identity
                    # fields. Path.lstat() obtains the complete no-follow data.
                    metadata = path.lstat()
                except OSError as error:
                    raise SandboxError("leased tree entry metadata failed") from error
                if directory_entry.is_symlink() or _metadata_is_reparse(metadata):
                    raise SandboxError("handle lease rejects links and reparse points")
                if stat.S_ISDIR(metadata.st_mode):
                    paths[relative_text] = path
                    pending.append((relative, path))
                    continue
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise SandboxError("handle lease accepts only independent files")
                total_bytes += metadata.st_size
                if total_bytes > self._max_total_bytes:
                    raise SandboxError("handle lease exceeds its byte limit")
                paths[relative_text] = path
        return paths

    def _open_entry(self, relative_path: str, path: Path) -> _LeaseEntry:
        try:
            if self._windows:
                assert self._api is not None
                flags = _FILE_FLAG_OPEN_REPARSE_POINT
                if path.is_dir():
                    flags |= _FILE_FLAG_BACKUP_SEMANTICS
                share_mode = _FILE_SHARE_READ
                if self._writable_root:
                    share_mode |= _FILE_SHARE_WRITE
                handle = self._api.open_path(
                    path,
                    share_mode=share_mode,
                    flags=flags,
                )
                identity: _HandleIdentity | _PortableIdentity = self._api.query(handle)
            else:
                flags = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0))
                flags |= int(getattr(os, "O_NOFOLLOW", 0))
                if path.is_dir():
                    flags |= int(getattr(os, "O_DIRECTORY", 0))
                handle = os.open(path, flags)
                identity = _portable_identity(os.fstat(handle))
        except OSError as error:
            raise SandboxError("leased path could not be opened") from error
        if (
            identity.links < 1
            or (not identity.is_directory and identity.links != 1)
            or isinstance(identity, _HandleIdentity)
            and (identity.is_reparse or identity.index < 1)
        ):
            try:
                if self._windows:
                    assert self._api is not None
                    self._api.close(handle)
                else:
                    os.close(handle)
            except OSError as close_error:
                raise SandboxError("invalid lease handle also failed to close") from (
                    close_error
                )
            raise SandboxError("leased path identity is unsafe")
        return _LeaseEntry(relative_path, path, handle, identity)

    def _validate_one(self, entry: _LeaseEntry) -> None:
        temporary_handle: int | None = None
        held: _HandleIdentity | _PortableIdentity
        current: _HandleIdentity | _PortableIdentity
        try:
            if self._windows:
                assert self._api is not None
                held = self._api.query(entry.handle)
                flags = _FILE_FLAG_OPEN_REPARSE_POINT
                if entry.identity.is_directory:
                    flags |= _FILE_FLAG_BACKUP_SEMANTICS
                temporary_handle = self._api.open_path(
                    entry.path,
                    share_mode=(
                        _FILE_SHARE_READ | _FILE_SHARE_WRITE
                        if self._writable_root
                        else _FILE_SHARE_READ
                    ),
                    flags=flags,
                )
                current = self._api.query(temporary_handle)
            else:
                held = _portable_identity(os.fstat(entry.handle))
                current = _portable_identity(entry.path.lstat())
        except OSError as error:
            raise SandboxError("leased identity validation failed") from error
        finally:
            if temporary_handle is not None:
                try:
                    assert self._api is not None
                    self._api.close(temporary_handle)
                except OSError as error:
                    raise SandboxError(
                        "temporary identity handle could not be closed"
                    ) from error
        if not _identity_matches(
            entry.identity,
            held,
            mutable_directory=self._writable_root,
        ) or not _identity_matches(
            entry.identity,
            current,
            mutable_directory=self._writable_root,
        ):
            raise SandboxError("leased path identity changed during execution")


def acquire_bounded_tree_lease(
    root: Path,
    *,
    writable_root: bool = False,
    expected_names: frozenset[str] | None = None,
    max_entries: int,
    max_total_bytes: int,
) -> HandleLease:
    """Acquire the default platform handle lease used by the WSB executor."""

    return BoundedTreeLease(
        root,
        writable_root=writable_root,
        expected_names=expected_names,
        max_entries=max_entries,
        max_total_bytes=max_total_bytes,
    )


def _portable_identity(metadata: os.stat_result) -> _PortableIdentity:
    return _PortableIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        links=metadata.st_nlink,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _identity_matches(
    expected: _HandleIdentity | _PortableIdentity,
    actual: _HandleIdentity | _PortableIdentity,
    *,
    mutable_directory: bool,
) -> bool:
    if type(expected) is not type(actual):
        return False
    if not mutable_directory:
        return expected == actual
    if isinstance(expected, _HandleIdentity) and isinstance(actual, _HandleIdentity):
        return (
            expected.volume == actual.volume
            and expected.index == actual.index
            and expected.attributes == actual.attributes
            and expected.links == actual.links
            and expected.is_directory
        )
    if isinstance(expected, _PortableIdentity) and isinstance(
        actual, _PortableIdentity
    ):
        return (
            expected.device == actual.device
            and expected.inode == actual.inode
            and expected.mode == actual.mode
            and expected.links == actual.links
            and expected.is_directory
        )
    return False


def _metadata_is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _path_has_reparse_component(path: Path) -> bool:
    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError:
            return True
        if _metadata_is_reparse(metadata) or current.is_symlink():
            return True
    return False
