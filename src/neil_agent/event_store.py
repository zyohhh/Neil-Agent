"""Optional, bounded JSONL persistence for metadata-only runtime events."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from pydantic import ValidationError

from .errors import EventStoreError
from .events import EventBus, EventSubscription, RuntimeEvent

EVENT_STORE_DIRECTORY = Path(".neil-agent") / "runtime-events"
EVENT_STORE_FILENAME = "events.jsonl"
EVENT_STORE_BACKUP_FILENAME = "events.jsonl.1"
EVENT_STORE_LOCK_FILENAME = "events.lock"
DEFAULT_EVENT_STORE_MAX_BYTES = 5_000_000
MIN_EVENT_STORE_MAX_BYTES = 10_000
MAX_EVENT_STORE_RECORD_BYTES = 8_192
EVENT_STORE_LOCK_TIMEOUT_SECONDS = 2.0
EVENT_STORE_LOCK_POLL_SECONDS = 0.05


class JsonlEventStore:
    """Persist runtime events only when explicitly registered or called."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_bytes: int = DEFAULT_EVENT_STORE_MAX_BYTES,
        lock_timeout: float = EVENT_STORE_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise EventStoreError("运行时事件工作区不是有效目录。")
        if max_bytes < MIN_EVENT_STORE_MAX_BYTES:
            raise EventStoreError(
                f"运行时事件文件上限不能小于 {MIN_EVENT_STORE_MAX_BYTES} 字节。"
            )
        if lock_timeout <= 0:
            raise EventStoreError("运行时事件锁超时必须大于 0 秒。")
        self._workspace_root = root
        self._store_root = root / EVENT_STORE_DIRECTORY
        self._max_bytes = max_bytes
        self._lock_timeout = lock_timeout

    @property
    def path(self) -> Path:
        return self._store_root / EVENT_STORE_FILENAME

    @property
    def backup_path(self) -> Path:
        return self._store_root / EVENT_STORE_BACKUP_FILENAME

    def register(self, bus: EventBus) -> EventSubscription:
        """Explicitly enable persistence as one isolated bus observer."""

        self.validate()
        return bus.subscribe(self.record)

    def validate(self) -> None:
        """Create the private directory and reject unsafe existing paths."""

        root = self._resolved_store_root(create=True)
        with self._lock(root, create=True):
            self._regular_file_size(root / EVENT_STORE_FILENAME)
            self._regular_file_size(root / EVENT_STORE_BACKUP_FILENAME)

    def record(self, event: RuntimeEvent) -> None:
        """Append one already-redacted event with a bounded durable write."""

        if not isinstance(event, RuntimeEvent):
            raise EventStoreError("运行时事件存储只接受 RuntimeEvent。")
        line = (event.model_dump_json() + "\n").encode("utf-8")
        if len(line) > MAX_EVENT_STORE_RECORD_BYTES:
            raise EventStoreError("运行时事件超过单条记录大小上限。")
        self._append(line)

    def load(self, *, include_backup: bool = True) -> tuple[RuntimeEvent, ...]:
        """Strictly load the retained event window in append order."""

        root = self._resolved_store_root(create=False)
        with self._lock(root, create=False):
            paths = (
                (
                    root / EVENT_STORE_BACKUP_FILENAME,
                    root / EVENT_STORE_FILENAME,
                )
                if include_backup
                else (root / EVENT_STORE_FILENAME,)
            )
            return tuple(event for path in paths for event in self._read_file(path))

    def _append(self, line: bytes) -> None:
        root = self._resolved_store_root(create=True)
        with self._lock(root, create=True):
            target = root / EVENT_STORE_FILENAME
            backup = root / EVENT_STORE_BACKUP_FILENAME
            current_size = self._regular_file_size(target)
            if current_size and current_size + len(line) > self._max_bytes:
                self._rotate(target, backup)
            self._write_line(target, line)

    def _resolved_store_root(self, *, create: bool) -> Path:
        try:
            if create:
                self._store_root.mkdir(parents=True, exist_ok=True)
            resolved = self._store_root.resolve(strict=True)
        except FileNotFoundError as error:
            raise EventStoreError("运行时事件目录不存在。") from error
        except OSError as error:
            raise EventStoreError("无法创建或访问运行时事件目录。") from error
        if resolved != self._store_root or not resolved.is_dir():
            raise EventStoreError("运行时事件目录必须是工作区内的真实目录。")
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as error:
            raise EventStoreError("运行时事件目录越过工作区边界。") from error
        return resolved

    def _lock(self, root: Path, *, create: bool) -> _EventStoreFileLock:
        return _EventStoreFileLock(
            root / EVENT_STORE_LOCK_FILENAME,
            timeout=self._lock_timeout,
            create=create,
        )

    @staticmethod
    def _regular_file_size(path: Path) -> int:
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            return 0
        except OSError as error:
            raise EventStoreError("无法检查运行时事件文件。") from error
        if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
            raise EventStoreError("运行时事件目标必须是真实普通文件。")
        return file_stat.st_size

    @staticmethod
    def _write_line(target: Path, line: bytes) -> None:
        descriptor = -1
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise EventStoreError("运行时事件目标必须是真实普通文件。")
            with os.fdopen(descriptor, "ab", closefd=True) as output:
                descriptor = -1
                output.write(line)
                output.flush()
                os.fsync(output.fileno())
        except EventStoreError:
            raise
        except OSError as error:
            raise EventStoreError("运行时事件写入失败。") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def _rotate(self, target: Path, backup: Path) -> None:
        self._regular_file_size(target)
        backup_size = self._regular_file_size(backup)
        try:
            if backup_size:
                backup.unlink()
            os.replace(target, backup)
        except OSError as error:
            raise EventStoreError("运行时事件轮转失败。") from error

    def _read_file(self, path: Path) -> tuple[RuntimeEvent, ...]:
        if self._regular_file_size(path) == 0:
            return ()
        descriptor = -1
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        events: list[RuntimeEvent] = []
        try:
            descriptor = os.open(path, flags)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise EventStoreError("运行时事件目标必须是真实普通文件。")
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                descriptor = -1
                while line := source.readline(MAX_EVENT_STORE_RECORD_BYTES + 1):
                    if len(line) > MAX_EVENT_STORE_RECORD_BYTES:
                        raise EventStoreError("运行时事件记录超过大小上限。")
                    if not line.endswith(b"\n"):
                        raise EventStoreError("运行时事件文件包含不完整记录。")
                    try:
                        events.append(RuntimeEvent.model_validate_json(line))
                    except ValidationError as error:
                        raise EventStoreError("运行时事件记录格式无效。") from error
        except EventStoreError:
            raise
        except OSError as error:
            raise EventStoreError("运行时事件读取失败。") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)
        return tuple(events)


class _EventStoreFileLock:
    """Kernel-owned cross-process lock for append, rotation, and replay."""

    def __init__(self, path: Path, *, timeout: float, create: bool) -> None:
        self._path = path
        self._timeout = timeout
        self._create = create
        self._descriptor = -1
        self._acquired = False

    def __enter__(self) -> _EventStoreFileLock:
        if not self.acquire():
            self.close()
            raise EventStoreError(f"运行时事件锁在 {self._timeout:g} 秒内不可用。")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def acquire(self) -> bool:
        if self._descriptor != -1:
            raise EventStoreError("运行时事件锁不能重复获取。")
        self._descriptor = self._open()
        deadline = monotonic() + self._timeout
        while True:
            try:
                if _try_lock_descriptor(self._descriptor):
                    self._acquired = True
                    return True
            except OSError as error:
                self.close()
                raise EventStoreError("运行时事件锁获取失败。") from error
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            sleep(min(EVENT_STORE_LOCK_POLL_SECONDS, remaining))

    def close(self) -> None:
        release_error: OSError | None = None
        if self._descriptor != -1 and self._acquired:
            try:
                _unlock_descriptor(self._descriptor)
            except OSError as error:
                release_error = error
            self._acquired = False
        if self._descriptor != -1:
            os.close(self._descriptor)
            self._descriptor = -1
        if release_error is not None:
            raise EventStoreError("运行时事件锁释放失败。") from release_error

    def _open(self) -> int:
        try:
            lock_stat = self._path.lstat()
        except FileNotFoundError:
            if not self._create:
                raise EventStoreError("运行时事件锁文件不存在。") from None
        except OSError as error:
            raise EventStoreError("无法检查运行时事件锁文件。") from error
        else:
            if self._path.is_symlink() or not stat.S_ISREG(lock_stat.st_mode):
                raise EventStoreError("运行时事件锁必须是真实普通文件。")

        descriptor = -1
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if self._create:
            flags |= os.O_CREAT
        try:
            descriptor = os.open(self._path, flags, 0o600)
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise EventStoreError("运行时事件锁必须是真实普通文件。")
            if file_stat.st_size == 0:
                if not self._create:
                    raise EventStoreError("运行时事件锁文件无效。")
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            return descriptor
        except EventStoreError:
            if descriptor != -1:
                os.close(descriptor)
            raise
        except FileNotFoundError as error:
            raise EventStoreError("运行时事件锁文件不存在。") from error
        except OSError as error:
            if descriptor != -1:
                os.close(descriptor)
            raise EventStoreError("运行时事件锁文件不可用。") from error


def _try_lock_descriptor(descriptor: int) -> bool:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True

    fcntl: Any = __import__("fcntl")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_descriptor(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    fcntl: Any = __import__("fcntl")
    fcntl.flock(descriptor, fcntl.LOCK_UN)
