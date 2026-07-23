"""Bounded metadata-only JSONL audit records for trusted lifecycle hooks."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .activity import safe_tool_name
from .errors import AuditError
from .hooks import HookEvent, LifecycleHooks

AUDIT_DIRECTORY = Path(".neil-agent") / "audit"
AUDIT_FILENAME = "events.jsonl"
AUDIT_BACKUP_FILENAME = "events.jsonl.1"
AUDIT_RECORD_VERSION = 1
MAX_AUDIT_RECORD_BYTES = 4_096


class JsonlAuditSink:
    """Append bounded lifecycle metadata without conversation or tool content."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_bytes: int = 1_000_000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise AuditError("审计日志工作区不是有效目录。")
        if max_bytes < 10_000:
            raise AuditError("审计日志大小上限不能小于 10000 字节。")
        self._workspace_root = root
        self._audit_root = root / AUDIT_DIRECTORY
        self._max_bytes = max_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))

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
        self._regular_file_size(root / AUDIT_FILENAME)
        self._regular_file_size(root / AUDIT_BACKUP_FILENAME)

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
        target = root / AUDIT_FILENAME
        backup = root / AUDIT_BACKUP_FILENAME
        current_size = self._regular_file_size(target)
        if current_size and current_size + len(line) > self._max_bytes:
            self._rotate(target, backup)
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

    def _resolved_audit_root(self) -> Path:
        try:
            self._audit_root.mkdir(parents=True, exist_ok=True)
            resolved = self._audit_root.resolve(strict=True)
        except OSError as error:
            raise AuditError("无法创建或访问审计日志目录。") from error
        if resolved != self._audit_root or not resolved.is_dir():
            raise AuditError("审计日志目录必须是工作区内的真实目录。")
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as error:
            raise AuditError("审计日志目录越过工作区边界。") from error
        return resolved

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
