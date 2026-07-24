"""Bounded metadata-only JSONL audit records for trusted lifecycle hooks."""

from __future__ import annotations

import errno
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from .activity import safe_tool_name
from .errors import AuditError
from .hooks import HookEvent, LifecycleHooks

AUDIT_DIRECTORY = Path(".neil-agent") / "audit"
AUDIT_FILENAME = "events.jsonl"
AUDIT_BACKUP_FILENAME = "events.jsonl.1"
AUDIT_LOCK_FILENAME = "events.lock"
AUDIT_RECORD_VERSION = 1
MAX_AUDIT_RECORD_BYTES = 4_096
AUDIT_LOCK_TIMEOUT_SECONDS = 2.0
AUDIT_LOCK_POLL_SECONDS = 0.05
AUDIT_STAGES = frozenset({"before_model", "after_model", "before_tool", "after_tool"})


@dataclass(frozen=True, slots=True)
class AuditLogStatus:
    """Read-only audit health metadata for ``/doctor``."""

    path: Path
    current_size_bytes: int
    backup_size_bytes: int
    max_bytes: int
    current_records: int | None
    backup_records: int | None
    invalid_records: int | None
    lock_available: bool


class JsonlAuditSink:
    """Append bounded lifecycle metadata without conversation or tool content."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_bytes: int = 1_000_000,
        clock: Callable[[], datetime] | None = None,
        lock_timeout: float = AUDIT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise AuditError("审计日志工作区不是有效目录。")
        if max_bytes < 10_000:
            raise AuditError("审计日志大小上限不能小于 10000 字节。")
        if lock_timeout <= 0:
            raise AuditError("审计日志锁超时必须大于 0 秒。")
        self._workspace_root = root
        self._audit_root = root / AUDIT_DIRECTORY
        self._max_bytes = max_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock_timeout = lock_timeout

    @property
    def path(self) -> Path:
        return self._audit_root / AUDIT_FILENAME

    def register(self, hooks: LifecycleHooks) -> None:
        """Attach the same metadata recorder to every supported hook stage."""

        self.validate()
        for stage in ("before_model", "after_model", "before_tool", "after_tool"):
            hooks.register(stage, self.record)

    def validate(self) -> None:
        """Create the audit directory and reject unsafe existing log paths."""

        root = self._resolved_audit_root()
        with self._lock(root, create=True):
            self._regular_file_size(root / AUDIT_FILENAME)
            self._regular_file_size(root / AUDIT_BACKUP_FILENAME)

    def inspect(self) -> AuditLogStatus:
        """Inspect paths, sizes, JSONL shape, and lock state without writing."""

        root = self._resolved_audit_root(create=False)
        target = root / AUDIT_FILENAME
        backup = root / AUDIT_BACKUP_FILENAME
        current_size = self._regular_file_size(target)
        backup_size = self._regular_file_size(backup)
        lock = self._lock(root, create=False, timeout=0)
        if not lock.acquire():
            lock.close()
            return AuditLogStatus(
                path=target,
                current_size_bytes=current_size,
                backup_size_bytes=backup_size,
                max_bytes=self._max_bytes,
                current_records=None,
                backup_records=None,
                invalid_records=None,
                lock_available=False,
            )
        try:
            current_size = self._regular_file_size(target)
            backup_size = self._regular_file_size(backup)
            current_records, current_invalid = self._inspect_records(target)
            backup_records, backup_invalid = self._inspect_records(backup)
        finally:
            lock.close()
        return AuditLogStatus(
            path=target,
            current_size_bytes=current_size,
            backup_size_bytes=backup_size,
            max_bytes=self._max_bytes,
            current_records=current_records,
            backup_records=backup_records,
            invalid_records=current_invalid + backup_invalid,
            lock_available=True,
        )

    def record(self, event: HookEvent) -> None:
        """Serialize one event after reducing it to safe numeric metadata."""

        payload = self._event_payload(event)
        line = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(line) > MAX_AUDIT_RECORD_BYTES:
            raise AuditError("审计事件超过单条大小上限。")
        self._append(line)

    def _event_payload(self, event: HookEvent) -> dict[str, Any]:
        timestamp = self._clock()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise AuditError("审计日志时间必须包含时区。")
        payload: dict[str, Any] = {
            "version": AUDIT_RECORD_VERSION,
            "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
            "stage": event.stage,
            "model_round": event.model_round,
            "message_count": event.message_count,
        }
        if event.model_response is not None:
            response = event.model_response
            response_metadata: dict[str, Any] = {
                "text_chars": len(response.content),
                "thinking_blocks": len(response.thinking),
                "tool_calls": len(response.tool_calls),
            }
            if response.usage is not None:
                response_metadata["usage"] = response.usage.model_dump()
            payload["model_response"] = response_metadata
        if event.tool_call is not None:
            payload["tool"] = {
                "name": safe_tool_name(event.tool_call.name),
                "argument_count": len(event.tool_call.arguments),
            }
        if event.tool_result is not None:
            payload["tool_result"] = {
                "is_error": event.tool_result.is_error,
                "content_chars": len(event.tool_result.content),
            }
        return payload

    def _append(self, line: bytes) -> None:
        root = self._resolved_audit_root()
        with self._lock(root, create=True):
            target = root / AUDIT_FILENAME
            backup = root / AUDIT_BACKUP_FILENAME
            current_size = self._regular_file_size(target)
            if current_size and current_size + len(line) > self._max_bytes:
                self._rotate(target, backup)
            self._write_line(target, line)

    @staticmethod
    def _write_line(target: Path, line: bytes) -> None:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(target, flags, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise AuditError("审计日志目标必须是真实普通文件。")
            with os.fdopen(descriptor, "ab", closefd=True) as output:
                descriptor = -1
                output.write(line)
                output.flush()
                os.fsync(output.fileno())
        except AuditError:
            raise
        except OSError as error:
            raise AuditError("审计日志写入失败。") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def _resolved_audit_root(self, *, create: bool = True) -> Path:
        try:
            if create:
                self._audit_root.mkdir(parents=True, exist_ok=True)
            resolved = self._audit_root.resolve(strict=True)
        except FileNotFoundError as error:
            raise AuditError("审计日志目录不存在。") from error
        except OSError as error:
            raise AuditError("无法创建或访问审计日志目录。") from error
        if resolved != self._audit_root or not resolved.is_dir():
            raise AuditError("审计日志目录必须是工作区内的真实目录。")
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as error:
            raise AuditError("审计日志目录越过工作区边界。") from error
        return resolved

    def _lock(
        self,
        root: Path,
        *,
        create: bool,
        timeout: float | None = None,
    ) -> _AuditFileLock:
        return _AuditFileLock(
            root / AUDIT_LOCK_FILENAME,
            timeout=self._lock_timeout if timeout is None else timeout,
            create=create,
        )

    @staticmethod
    def _regular_file_size(path: Path) -> int:
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            return 0
        except OSError as error:
            raise AuditError("无法检查审计日志文件。") from error
        if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
            raise AuditError("审计日志目标必须是真实普通文件。")
        return file_stat.st_size

    def _rotate(self, target: Path, backup: Path) -> None:
        self._regular_file_size(target)
        backup_size = self._regular_file_size(backup)
        try:
            if backup_size:
                backup.unlink()
            os.replace(target, backup)
        except OSError as error:
            raise AuditError("审计日志轮转失败。") from error

    def _inspect_records(self, path: Path) -> tuple[int, int]:
        if self._regular_file_size(path) == 0:
            return 0, 0
        descriptor = -1
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        records = 0
        invalid = 0
        try:
            descriptor = os.open(path, flags)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise AuditError("审计日志目标必须是真实普通文件。")
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                descriptor = -1
                while line := source.readline(MAX_AUDIT_RECORD_BYTES + 1):
                    records += 1
                    if len(line) > MAX_AUDIT_RECORD_BYTES:
                        invalid += 1
                        while line and not line.endswith(b"\n"):
                            line = source.readline(MAX_AUDIT_RECORD_BYTES + 1)
                        continue
                    try:
                        payload = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        invalid += 1
                        continue
                    if (
                        not isinstance(payload, dict)
                        or payload.get("version") != AUDIT_RECORD_VERSION
                        or payload.get("stage") not in AUDIT_STAGES
                    ):
                        invalid += 1
        except AuditError:
            raise
        except OSError as error:
            raise AuditError("审计日志读取失败。") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)
        return records, invalid


class _AuditFileLock:
    """Kernel-owned cross-process lock with bounded acquisition."""

    def __init__(self, path: Path, *, timeout: float, create: bool) -> None:
        self._path = path
        self._timeout = timeout
        self._create = create
        self._descriptor = -1
        self._acquired = False

    def __enter__(self) -> _AuditFileLock:
        if not self.acquire():
            self.close()
            raise AuditError(f"审计日志锁在 {self._timeout:g} 秒内不可用，已拒绝写入。")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def acquire(self) -> bool:
        if self._descriptor != -1:
            raise AuditError("审计日志锁不能重复获取。")
        self._descriptor = self._open()
        deadline = monotonic() + self._timeout
        while True:
            try:
                if _try_lock_descriptor(self._descriptor):
                    self._acquired = True
                    return True
            except OSError as error:
                self.close()
                raise AuditError("审计日志锁获取失败。") from error
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            sleep(min(AUDIT_LOCK_POLL_SECONDS, remaining))

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
            raise AuditError("审计日志锁释放失败。") from release_error

    def _open(self) -> int:
        try:
            lock_stat = self._path.lstat()
        except FileNotFoundError:
            if not self._create:
                raise AuditError("审计日志锁文件不存在。") from None
        except OSError as error:
            raise AuditError("无法检查审计日志锁文件。") from error
        else:
            if self._path.is_symlink() or not stat.S_ISREG(lock_stat.st_mode):
                raise AuditError("审计日志锁必须是真实普通文件。")

        descriptor = -1
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if self._create:
            flags |= os.O_CREAT
        try:
            descriptor = os.open(self._path, flags, 0o600)
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise AuditError("审计日志锁必须是真实普通文件。")
            if file_stat.st_size == 0:
                if not self._create:
                    raise AuditError("审计日志锁文件无效。")
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            return descriptor
        except AuditError:
            if descriptor != -1:
                os.close(descriptor)
            raise
        except FileNotFoundError as error:
            raise AuditError("审计日志锁文件不存在。") from error
        except OSError as error:
            if descriptor != -1:
                os.close(descriptor)
            raise AuditError("审计日志锁文件不可用。") from error


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
