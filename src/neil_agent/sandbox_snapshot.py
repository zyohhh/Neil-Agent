"""Filtered, deterministic workspace snapshots for sandbox preparation."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Literal, Protocol, Self

from .errors import SandboxError

SNAPSHOT_MANIFEST_VERSION = 1
COPY_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_ENTRIES = 100_000
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024

_BLOCKED_DIRECTORIES = frozenset(
    {
        ".agents",
        ".aws",
        ".azure",
        ".codex",
        ".docker",
        ".git",
        ".gnupg",
        ".kube",
        ".mypy_cache",
        ".neil-agent",
        ".pytest_cache",
        ".ruff_cache",
        ".ssh",
        ".venv",
        "__pycache__",
        "appdata",
        "node_modules",
    }
)
_BLOCKED_FILE_NAMES = frozenset(
    {
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "application_default_credentials.json",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
_BLOCKED_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
_REPARSE_POINT_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_FILE_SHARE_READ = 0x0001
_WINDOWS_FILE_SHARE_WRITE = 0x0002
_WINDOWS_FILE_SHARE_DELETE = 0x0004
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_WINDOWS_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


@dataclass(frozen=True, slots=True)
class _WindowsHandleInformation:
    file_attributes: int
    link_count: int
    volume_serial: int
    file_index: int

    @property
    def identity(self) -> tuple[int, int]:
        return self.volume_serial, self.file_index


class _WindowsSnapshotApi(Protocol):
    def open_path(
        self,
        path: Path,
        *,
        share_mode: int,
        flags: int,
    ) -> int:
        """Open a file or directory using CreateFileW."""

    def query(self, handle: int) -> _WindowsHandleInformation:
        """Return identity, link, and type metadata for one handle."""

    def to_fd(self, handle: int) -> int:
        """Transfer ownership of a file handle to a CRT descriptor."""

    def close(self, handle: int) -> None:
        """Close a handle or raise OSError."""


class _CtypesWindowsSnapshotApi:
    __slots__ = (
        "_close_handle",
        "_create_file",
        "_get_information",
        "_open_osfhandle",
    )

    def __init__(self) -> None:
        try:
            import msvcrt
        except ImportError as error:
            raise OSError("MSVCRT is unavailable") from error
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise OSError("Win32 APIs are unavailable")
        kernel32 = win_dll("kernel32", use_last_error=True)
        self._create_file = kernel32.CreateFileW
        self._create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._create_file.restype = wintypes.HANDLE
        self._get_information = kernel32.GetFileInformationByHandle
        self._get_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self._get_information.restype = wintypes.BOOL
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL
        self._open_osfhandle = msvcrt.open_osfhandle

    def open_path(
        self,
        path: Path,
        *,
        share_mode: int,
        flags: int,
    ) -> int:
        handle = self._create_file(
            str(path),
            _WINDOWS_GENERIC_READ,
            share_mode,
            None,
            _WINDOWS_OPEN_EXISTING,
            flags,
            None,
        )
        if handle in {None, _WINDOWS_INVALID_HANDLE}:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        return int(handle)

    def query(self, handle: int) -> _WindowsHandleInformation:
        information = _ByHandleFileInformation()
        if not self._get_information(handle, ctypes.byref(information)):
            raise OSError(
                ctypes.get_last_error(),
                "GetFileInformationByHandle failed",
            )
        file_index = int(information.nFileIndexHigh) << 32
        file_index |= int(information.nFileIndexLow)
        return _WindowsHandleInformation(
            file_attributes=int(information.dwFileAttributes),
            link_count=int(information.nNumberOfLinks),
            volume_serial=int(information.dwVolumeSerialNumber),
            file_index=file_index,
        )

    def to_fd(self, handle: int) -> int:
        descriptor_flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        descriptor_flags |= int(getattr(os, "O_NOINHERIT", 0))
        return int(self._open_osfhandle(handle, descriptor_flags))

    def close(self, handle: int) -> None:
        if not self._close_handle(handle):
            raise OSError(ctypes.get_last_error(), "CloseHandle failed")


class _WindowsDirectoryGuard:
    __slots__ = ("_api", "_handle", "_identity", "_path")

    def __init__(
        self,
        path: Path,
        *,
        api: _WindowsSnapshotApi | None = None,
    ) -> None:
        self._path = path
        self._api = _windows_snapshot_api() if api is None else api
        self._handle: int | None = None
        self._identity: tuple[int, int] | None = None

    def __enter__(self) -> Self:
        share_mode = _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE
        flags = (
            _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
        )
        try:
            handle = self._api.open_path(
                self._path,
                share_mode=share_mode,
                flags=flags,
            )
        except OSError as error:
            raise SandboxError("无法锁定工作区源目录进行快照遍历。") from error
        self._handle = handle
        information = self._query_or_close(
            "无法验证工作区源目录句柄。",
        )
        if (
            not (information.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
            or information.file_attributes & _REPARSE_POINT_ATTRIBUTE
            or information.link_count < 1
            or information.file_index < 1
        ):
            self._close_or_raise("拒绝源目录时无法可靠关闭句柄。")
            raise SandboxError("工作区源目录句柄类型、链接或身份无效。")
        self._identity = information.identity
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        validation_error: SandboxError | None = None
        try:
            information = self._query()
            if (
                information.identity != self._identity
                or not (information.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
                or information.file_attributes & _REPARSE_POINT_ATTRIBUTE
                or information.link_count < 1
                or information.file_index < 1
            ):
                validation_error = SandboxError("工作区源目录句柄在遍历期间发生变化。")
        except SandboxError as error:
            validation_error = error
        self._close_or_raise("无法可靠关闭工作区源目录 guard。")
        if validation_error is not None:
            raise validation_error

    def _query(self) -> _WindowsHandleInformation:
        if self._handle is None:
            raise SandboxError("工作区源目录 guard 未持有句柄。")
        try:
            return self._api.query(self._handle)
        except OSError as error:
            raise SandboxError("无法查询工作区源目录 guard。") from error

    def _query_or_close(self, message: str) -> _WindowsHandleInformation:
        try:
            return self._query()
        except SandboxError as error:
            self._close_or_raise("目录查询失败且无法可靠关闭句柄。")
            raise SandboxError(message) from error

    def _close_or_raise(self, message: str) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        try:
            self._api.close(handle)
        except OSError as error:
            raise SandboxError(message) from error


@lru_cache(maxsize=1)
def _windows_snapshot_api() -> _WindowsSnapshotApi:
    try:
        return _CtypesWindowsSnapshotApi()
    except OSError as error:
        raise SandboxError("当前 Windows 运行时缺少 Win32 文件 API。") from error


@dataclass(frozen=True, slots=True)
class SnapshotLimits:
    """Bound the number and bytes copied from one workspace."""

    max_entries: int = DEFAULT_MAX_ENTRIES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES

    def __post_init__(self) -> None:
        _positive_integer("snapshot entry limit", self.max_entries)
        _positive_integer("snapshot file limit", self.max_file_bytes)
        _positive_integer("snapshot total limit", self.max_total_bytes)
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("snapshot file limit cannot exceed total limit")


@dataclass(frozen=True, slots=True)
class SnapshotManifestEntry:
    """One deterministic relative path in a prepared snapshot."""

    path: str
    kind: Literal["directory", "file"]
    size_bytes: int
    sha256: str | None


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """Canonical snapshot inventory and its content digest."""

    entries: tuple[SnapshotManifestEntry, ...]
    file_count: int
    total_bytes: int
    canonical_json: str
    digest: str
    version: int = SNAPSHOT_MANIFEST_VERSION


@dataclass(slots=True)
class PreparedSnapshot:
    """An owned snapshot directory removed when its context closes."""

    root: Path
    manifest: SnapshotManifest
    _root_identity: os.stat_result
    _closed: bool = False

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> Self:
        if self._closed:
            raise SandboxError("已清理的工作区快照不能重新进入上下文。")
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self.close()

    def close(self) -> None:
        """Remove only the directory identity created by this builder."""

        if self._closed:
            return
        _remove_owned_snapshot(self.root, self._root_identity)
        self._closed = True


@dataclass(slots=True)
class _BuildState:
    limits: SnapshotLimits
    entries: list[SnapshotManifestEntry]
    entry_count: int = 0
    file_count: int = 0
    total_bytes: int = 0

    def account_directory(self, relative: str) -> None:
        self._account_entry()
        self.entries.append(
            SnapshotManifestEntry(
                path=relative,
                kind="directory",
                size_bytes=0,
                sha256=None,
            )
        )

    def validate_file_capacity(self, size_bytes: int) -> None:
        if self.entry_count >= self.limits.max_entries:
            raise SandboxError("工作区快照超过条目数量上限。")
        if size_bytes > self.limits.max_file_bytes:
            raise SandboxError("工作区快照包含超过单文件上限的文件。")
        if self.total_bytes + size_bytes > self.limits.max_total_bytes:
            raise SandboxError("工作区快照超过累计字节上限。")

    def account_file(self, relative: str, size_bytes: int, digest: str) -> None:
        self._account_entry()
        next_total = self.total_bytes + size_bytes
        self.file_count += 1
        self.total_bytes = next_total
        self.entries.append(
            SnapshotManifestEntry(
                path=relative,
                kind="file",
                size_bytes=size_bytes,
                sha256=digest,
            )
        )

    def _account_entry(self) -> None:
        if self.entry_count >= self.limits.max_entries:
            raise SandboxError("工作区快照超过条目数量上限。")
        self.entry_count += 1


def prepare_snapshot(
    source_root: Path,
    destination: Path,
    *,
    limits: SnapshotLimits | None = None,
) -> PreparedSnapshot:
    """Copy a filtered source tree into a new, exclusively owned directory."""

    source, target = _validate_roots(source_root, destination)
    effective_limits = SnapshotLimits() if limits is None else limits
    if not isinstance(effective_limits, SnapshotLimits):
        raise ValueError("snapshot limits must be SnapshotLimits")

    try:
        os.mkdir(target, mode=0o700)
    except FileExistsError as error:
        raise SandboxError("工作区快照目标必须不存在。") from error
    except OSError as error:
        raise SandboxError("无法独占创建工作区快照目标。") from error

    try:
        root_identity = target.lstat()
    except OSError as error:
        try:
            os.rmdir(target)
        except OSError as cleanup_error:
            raise SandboxError(
                "无法验证新建快照目录，且无法安全移除该空目录。"
            ) from cleanup_error
        raise SandboxError("无法验证新建快照目录。") from error

    try:
        if not stat.S_ISDIR(root_identity.st_mode) or _is_reparse(target):
            raise SandboxError("工作区快照目标创建后不是安全的真实目录。")
        state = _BuildState(limits=effective_limits, entries=[])
        _copy_directory(source, target, PurePosixPath("."), state)
        manifest = _build_manifest(state)
        return PreparedSnapshot(
            root=target,
            manifest=manifest,
            _root_identity=root_identity,
        )
    except BaseException as error:
        try:
            _remove_owned_snapshot(target, root_identity)
        except SandboxError as cleanup_error:
            raise SandboxError("工作区快照构建失败且临时目录无法安全清理。") from (
                cleanup_error
            )
        raise error


def inspect_prepared_snapshot(
    root: Path,
    *,
    limits: SnapshotLimits | None = None,
) -> SnapshotManifest:
    """Rebuild a manifest from an existing snapshot without following links.

    This is intentionally stricter than the source filter: a sensitive name
    appearing after preparation is rejected instead of silently omitted.
    """

    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("prepared snapshot root must be an absolute Path")
    effective_limits = SnapshotLimits() if limits is None else limits
    if not isinstance(effective_limits, SnapshotLimits):
        raise ValueError("snapshot limits must be SnapshotLimits")
    try:
        resolved = root.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as error:
        raise SandboxError("无法访问待复核的工作区快照。") from error
    if (
        resolved != root
        or resolved.parent == resolved
        or not stat.S_ISDIR(metadata.st_mode)
        or _path_has_reparse_component(resolved)
    ):
        raise SandboxError("待复核快照必须是真实、无重解析点的绝对非根目录。")

    state = _BuildState(limits=effective_limits, entries=[])
    _inspect_snapshot_directory(resolved, PurePosixPath("."), state)
    return _build_manifest(state)


def _inspect_snapshot_directory(
    directory: Path,
    relative: PurePosixPath,
    state: _BuildState,
    *,
    windows_api: _WindowsSnapshotApi | None = None,
) -> None:
    if os.name == "nt" or windows_api is not None:
        with _WindowsDirectoryGuard(directory, api=windows_api):
            _inspect_snapshot_directory_contents(
                directory,
                relative,
                state,
                windows_api=windows_api,
            )
        return
    _inspect_snapshot_directory_contents(
        directory,
        relative,
        state,
        windows_api=None,
    )


def _inspect_snapshot_directory_contents(
    directory: Path,
    relative: PurePosixPath,
    state: _BuildState,
    *,
    windows_api: _WindowsSnapshotApi | None,
) -> None:
    before_metadata = _safe_lstat(directory, "无法读取待复核快照目录元数据。")
    if not stat.S_ISDIR(before_metadata.st_mode) or _is_reparse(directory):
        raise SandboxError("待复核快照目录发生类型变化。")
    before_names = _directory_names(directory)

    for name in before_names:
        path = directory / name
        metadata = _safe_lstat(path, "待复核快照条目消失或不可读取。")
        is_directory = stat.S_ISDIR(metadata.st_mode)
        if (
            _metadata_is_reparse(metadata)
            or path.is_symlink()
            or _is_filtered_name(name, is_directory=is_directory)
        ):
            raise SandboxError("待复核快照包含敏感名称、链接或重解析点。")
        relative_path = (
            PurePosixPath(name) if relative == PurePosixPath(".") else relative / name
        )
        relative_text = relative_path.as_posix()
        if is_directory:
            state.account_directory(relative_text)
            _inspect_snapshot_directory(
                path,
                relative_path,
                state,
                windows_api=windows_api,
            )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SandboxError("待复核快照只接受普通文件和目录。")
        state.validate_file_capacity(metadata.st_size)
        size_bytes, digest = _inspect_snapshot_file(
            path,
            metadata,
            state.limits,
        )
        state.account_file(relative_text, size_bytes, digest)

    after_names = _directory_names(directory)
    after_metadata = _safe_lstat(directory, "无法复核快照目录元数据。")
    if before_names != after_names or not _same_metadata(
        before_metadata,
        after_metadata,
    ):
        raise SandboxError("待复核快照目录在扫描期间发生变化。")


def _inspect_snapshot_file(
    path: Path,
    before_metadata: os.stat_result,
    limits: SnapshotLimits,
) -> tuple[int, str]:
    descriptor = _open_source_no_follow(path)
    try:
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_nlink != 1
            or not os.path.samestat(before_metadata, opened_metadata)
            or not _same_metadata(before_metadata, opened_metadata)
        ):
            raise SandboxError("待复核快照文件在打开前发生替换或变化。")
        digest = hashlib.sha256()
        size_bytes = 0
        while True:
            chunk = os.read(descriptor, COPY_CHUNK_BYTES)
            if not chunk:
                break
            size_bytes += len(chunk)
            if (
                size_bytes > opened_metadata.st_size
                or size_bytes > limits.max_file_bytes
            ):
                raise SandboxError("待复核快照文件增长或超过上限。")
            digest.update(chunk)
        handle_after = os.fstat(descriptor)
        path_after = _safe_lstat(path, "待复核快照文件在扫描后消失。")
        if (
            size_bytes != opened_metadata.st_size
            or not os.path.samestat(opened_metadata, handle_after)
            or not os.path.samestat(opened_metadata, path_after)
            or not _same_metadata(opened_metadata, handle_after)
            or not _same_metadata(opened_metadata, path_after)
        ):
            raise SandboxError("待复核快照文件在扫描期间发生变化。")
        return size_bytes, digest.hexdigest()
    finally:
        os.close(descriptor)


def _validate_roots(source_root: Path, destination: Path) -> tuple[Path, Path]:
    if not isinstance(source_root, Path) or not isinstance(destination, Path):
        raise ValueError("snapshot source and destination must be Paths")
    if not source_root.is_absolute() or not destination.is_absolute():
        raise ValueError("snapshot source and destination must be absolute")
    if _path_has_reparse_component(source_root):
        raise SandboxError("工作区源根必须是真实目录，不能经过重解析点。")
    try:
        source = source_root.resolve(strict=True)
    except OSError as error:
        raise SandboxError("工作区源根不存在或无法解析。") from error
    if not source.is_dir() or _is_reparse(source) or source.parent == source:
        raise SandboxError("工作区源根必须是非卷根的真实普通目录。")

    if not destination.name or destination.parent == destination:
        raise SandboxError("工作区快照目标不能是卷根。")
    if _path_has_reparse_component(destination.parent):
        raise SandboxError("工作区快照目标父目录不能经过重解析点。")
    try:
        destination_parent = destination.parent.resolve(strict=True)
    except OSError as error:
        raise SandboxError("工作区快照目标父目录不存在或无法解析。") from error
    if not destination_parent.is_dir() or _is_reparse(destination_parent):
        raise SandboxError("工作区快照目标父路径必须是真实普通目录。")
    target = destination_parent / destination.name
    if os.path.lexists(target):
        raise SandboxError("工作区快照目标必须不存在。")
    try:
        target.relative_to(source)
    except ValueError:
        pass
    else:
        raise SandboxError("工作区快照目标不能位于源根内部。")
    return source, target


def _copy_directory(
    source: Path,
    destination: Path,
    relative: PurePosixPath,
    state: _BuildState,
    *,
    windows_api: _WindowsSnapshotApi | None = None,
) -> None:
    # Windows directory handles intentionally deny FILE_SHARE_DELETE while a
    # subtree is traversed. POSIX keeps its metadata/no-follow checks below,
    # but does not claim an equivalent Windows-style rename lock.
    if os.name == "nt" or windows_api is not None:
        with _WindowsDirectoryGuard(source, api=windows_api):
            _copy_directory_contents(
                source,
                destination,
                relative,
                state,
                windows_api=windows_api,
            )
        return
    _copy_directory_contents(
        source,
        destination,
        relative,
        state,
        windows_api=None,
    )


def _copy_directory_contents(
    source: Path,
    destination: Path,
    relative: PurePosixPath,
    state: _BuildState,
    *,
    windows_api: _WindowsSnapshotApi | None,
) -> None:
    before_metadata = _safe_lstat(source, "无法读取工作区源目录元数据。")
    if not stat.S_ISDIR(before_metadata.st_mode) or _is_reparse(source):
        raise SandboxError("工作区源目录在复制前发生类型变化。")
    before_names = _directory_names(source)

    for name in before_names:
        source_path = source / name
        destination_path = destination / name
        entry_metadata = _safe_lstat(
            source_path,
            "工作区源条目在复制前消失或不可读取。",
        )
        if _metadata_is_reparse(entry_metadata) or source_path.is_symlink():
            raise SandboxError("工作区快照拒绝符号链接、junction 或重解析点。")
        if _is_filtered_name(name, is_directory=stat.S_ISDIR(entry_metadata.st_mode)):
            continue

        relative_path = (
            PurePosixPath(name) if relative == PurePosixPath(".") else relative / name
        )
        relative_text = relative_path.as_posix()
        if stat.S_ISDIR(entry_metadata.st_mode):
            state.account_directory(relative_text)
            try:
                os.mkdir(destination_path, mode=0o700)
            except OSError as error:
                raise SandboxError("无法独占创建快照子目录。") from error
            _copy_directory(
                source_path,
                destination_path,
                relative_path,
                state,
                windows_api=windows_api,
            )
        elif stat.S_ISREG(entry_metadata.st_mode):
            state.validate_file_capacity(entry_metadata.st_size)
            size_bytes, digest = _copy_file(
                source_path,
                destination_path,
                entry_metadata,
                state.limits,
            )
            state.account_file(relative_text, size_bytes, digest)
        else:
            raise SandboxError("工作区快照只接受普通文件和目录。")

    after_names = _directory_names(source)
    after_metadata = _safe_lstat(source, "无法复核工作区源目录元数据。")
    if before_names != after_names or not _same_metadata(
        before_metadata,
        after_metadata,
    ):
        raise SandboxError("工作区源目录在快照期间发生变化。")


def _copy_file(
    source: Path,
    destination: Path,
    before_metadata: os.stat_result,
    limits: SnapshotLimits,
) -> tuple[int, str]:
    if before_metadata.st_size > limits.max_file_bytes:
        raise SandboxError("工作区快照包含超过单文件上限的文件。")
    source_fd = _open_source_no_follow(source)
    try:
        opened_metadata = os.fstat(source_fd)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or not os.path.samestat(before_metadata, opened_metadata)
            or not _same_metadata(before_metadata, opened_metadata)
        ):
            raise SandboxError("工作区源文件在打开前发生替换或变化。")
        if opened_metadata.st_nlink != 1:
            raise SandboxError("工作区快照拒绝硬链接文件。")

        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        destination_flags |= int(getattr(os, "O_BINARY", 0))
        destination_flags |= int(getattr(os, "O_NOINHERIT", 0))
        destination_flags |= int(getattr(os, "O_CLOEXEC", 0))
        try:
            destination_fd = os.open(destination, destination_flags, 0o600)
        except OSError as error:
            raise SandboxError("无法独占创建快照文件。") from error
        try:
            size_bytes, digest = _stream_copy(
                source_fd,
                destination_fd,
                expected_size=opened_metadata.st_size,
                limits=limits,
            )
            os.fsync(destination_fd)
        except OSError as error:
            raise SandboxError("工作区快照文件复制失败。") from error
        finally:
            os.close(destination_fd)

        handle_after = os.fstat(source_fd)
        path_after = _safe_lstat(source, "工作区源文件在复核前消失。")
        if (
            not os.path.samestat(opened_metadata, handle_after)
            or not os.path.samestat(opened_metadata, path_after)
            or not _same_metadata(opened_metadata, handle_after)
            or not _same_metadata(opened_metadata, path_after)
        ):
            raise SandboxError("工作区源文件在快照期间发生变化。")
        return size_bytes, digest
    finally:
        os.close(source_fd)


def _stream_copy(
    source_fd: int,
    destination_fd: int,
    *,
    expected_size: int,
    limits: SnapshotLimits,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    copied = 0
    while True:
        chunk = os.read(source_fd, COPY_CHUNK_BYTES)
        if not chunk:
            break
        copied += len(chunk)
        if copied > expected_size or copied > limits.max_file_bytes:
            raise SandboxError("工作区源文件在复制期间增长或超过上限。")
        digest.update(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(destination_fd, view)
            if written <= 0:
                raise OSError("snapshot write made no progress")
            view = view[written:]
    if copied != expected_size:
        raise SandboxError("工作区源文件在复制期间发生长度变化。")
    return copied, digest.hexdigest()


def _open_source_no_follow(path: Path) -> int:
    if os.name == "nt":
        return _open_windows_source_no_follow(path)
    flags = os.O_RDONLY
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise SandboxError("当前平台缺少文件 no-follow 打开能力。")
    flags |= int(no_follow)
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    try:
        return os.open(path, flags)
    except OSError as error:
        raise SandboxError("无法以 no-follow 方式打开工作区源文件。") from error


def _open_windows_source_no_follow(
    path: Path,
    *,
    api: _WindowsSnapshotApi | None = None,
) -> int:
    windows_api = _windows_snapshot_api() if api is None else api
    share_mode = _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE
    try:
        handle = windows_api.open_path(
            path,
            share_mode=share_mode,
            flags=_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        )
    except OSError as error:
        raise SandboxError("无法以 no-follow 方式打开工作区源文件。") from error
    try:
        information = windows_api.query(handle)
    except OSError as error:
        try:
            windows_api.close(handle)
        except OSError as close_error:
            raise SandboxError("源文件查询失败且无法可靠关闭句柄。") from close_error
        raise SandboxError("无法验证工作区源文件句柄。") from error
    if (
        information.file_attributes & _REPARSE_POINT_ATTRIBUTE
        or information.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY
        or information.link_count != 1
        or information.file_index < 1
    ):
        try:
            windows_api.close(handle)
        except OSError as error:
            raise SandboxError("拒绝源文件时无法可靠关闭句柄。") from error
        raise SandboxError("工作区快照拒绝重解析点、目录或硬链接文件。")

    try:
        descriptor = windows_api.to_fd(handle)
    except (OSError, ValueError) as error:
        try:
            windows_api.close(handle)
        except OSError as close_error:
            raise SandboxError("源文件句柄转换失败且无法可靠关闭。") from close_error
        raise SandboxError("无法把安全 Windows 句柄转换为文件描述符。") from error
    if descriptor < 0:
        try:
            windows_api.close(handle)
        except OSError as error:
            raise SandboxError("源文件句柄转换失败且无法可靠关闭。") from error
        raise SandboxError("安全 Windows 句柄转换返回了无效描述符。")
    # Ownership has moved to the CRT descriptor. Its caller closes the handle
    # through os.close().
    return descriptor


def _directory_names(directory: Path) -> tuple[str, ...]:
    try:
        with os.scandir(directory) as entries:
            names = [entry.name for entry in entries]
    except OSError as error:
        raise SandboxError("无法列举工作区源目录。") from error
    return tuple(sorted(names, key=lambda name: (name.casefold(), name)))


def _same_metadata(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        os.path.samestat(before, after)
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
    )


def _safe_lstat(path: Path, message: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as error:
        raise SandboxError(message) from error


def _metadata_is_reparse(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    return _metadata_is_reparse(metadata) or path.is_symlink()


def _path_has_reparse_component(path: Path) -> bool:
    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if _is_reparse(current):
            return True
    return False


def _is_filtered_name(name: str, *, is_directory: bool) -> bool:
    lowered = name.casefold()
    if is_directory and lowered in _BLOCKED_DIRECTORIES:
        return True
    return (
        lowered == ".env"
        or (lowered.startswith(".env.") and lowered != ".env.example")
        or lowered in _BLOCKED_FILE_NAMES
        or Path(lowered).suffix in _BLOCKED_SUFFIXES
    )


def _build_manifest(state: _BuildState) -> SnapshotManifest:
    entries = tuple(sorted(state.entries, key=lambda entry: entry.path.encode("utf-8")))
    payload = {
        "entries": [
            {
                "kind": entry.kind,
                "path": entry.path,
                "sha256": entry.sha256,
                "size_bytes": entry.size_bytes,
            }
            for entry in entries
        ],
        "file_count": state.file_count,
        "total_bytes": state.total_bytes,
        "version": SNAPSHOT_MANIFEST_VERSION,
    }
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return SnapshotManifest(
        entries=entries,
        file_count=state.file_count,
        total_bytes=state.total_bytes,
        canonical_json=canonical_json,
        digest=digest,
    )


def _remove_owned_snapshot(root: Path, identity: os.stat_result) -> None:
    if not os.path.lexists(root):
        return
    _remove_owned_directory(root, identity, is_root=True)


def _remove_owned_directory(
    directory: Path,
    identity: os.stat_result,
    *,
    is_root: bool = False,
    windows_api: _WindowsSnapshotApi | None = None,
) -> None:
    if os.name == "nt" or windows_api is not None:
        with _WindowsDirectoryGuard(directory, api=windows_api):
            _remove_owned_directory_contents(
                directory,
                identity,
                is_root=is_root,
                windows_api=windows_api,
            )
    else:
        _remove_owned_directory_contents(
            directory,
            identity,
            is_root=is_root,
            windows_api=None,
        )

    # The Windows guard must be closed before rmdir because it denies delete
    # sharing. Revalidate the path identity first; a last-instant junction
    # substitution can only make rmdir remove the junction itself, not recurse.
    _verify_owned_directory(directory, identity, is_root=is_root)
    try:
        os.rmdir(directory)
    except OSError as error:
        raise SandboxError("无法清理工作区快照目录。") from error


def _remove_owned_directory_contents(
    directory: Path,
    identity: os.stat_result,
    *,
    is_root: bool,
    windows_api: _WindowsSnapshotApi | None,
) -> None:
    _verify_owned_directory(directory, identity, is_root=is_root)

    try:
        with os.scandir(directory) as entries:
            names = sorted(
                (entry.name for entry in entries),
                key=lambda name: (name.casefold(), name),
            )
    except OSError as error:
        raise SandboxError("无法列举待清理的工作区快照目录。") from error

    for name in names:
        _verify_owned_directory(directory, identity, is_root=is_root)
        path = directory / name
        metadata = _safe_lstat(path, "无法验证待清理的工作区快照条目。")
        if _metadata_is_reparse(metadata) or path.is_symlink():
            raise SandboxError("清理期间发现重解析点，已拒绝递归清理。")
        if stat.S_ISDIR(metadata.st_mode):
            _remove_owned_directory(
                path,
                metadata,
                windows_api=windows_api,
            )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SandboxError("清理期间发现非普通快照条目，已拒绝清理。")
        try:
            os.unlink(path)
        except OSError as error:
            raise SandboxError("无法清理工作区快照文件。") from error


def _verify_owned_directory(
    directory: Path,
    identity: os.stat_result,
    *,
    is_root: bool,
) -> None:
    current = _safe_lstat(directory, "无法验证待清理的工作区快照目录。")
    if _metadata_is_reparse(current):
        raise SandboxError("清理期间发现重解析点，已拒绝递归清理。")
    if not stat.S_ISDIR(current.st_mode) or not os.path.samestat(identity, current):
        scope = "根目录" if is_root else "子目录"
        raise SandboxError(f"工作区快照{scope}身份已变化，拒绝递归清理。")


def _positive_integer(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
