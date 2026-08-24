"""Bounded in-memory task checkpoints for Neil Agent file edits."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256

from .errors import ToolError

MAX_FILE_CHECKPOINTS = 20
MAX_CHECKPOINT_CONTENT_CHARS = 5_000_000
MAX_FILES_PER_CHECKPOINT = 50
MAX_CHECKPOINT_PATH_CHARS = 1_000


@dataclass(frozen=True, slots=True)
class FileEditCheckpoint:
    """Original content and final identity for one path in an Agent task."""

    path: str
    resulting_hash: str
    resulting_chars: int
    original_content: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class FileTaskCheckpoint:
    """All effective Agent file edits made during one user-request turn."""

    checkpoint_id: str
    created_at: datetime
    edits: tuple[FileEditCheckpoint, ...]

    @property
    def file_count(self) -> int:
        return len(self.edits)


@dataclass(frozen=True, slots=True)
class PreparedFileRestoreEntry:
    """One restore target tied to the exact content seen during preview."""

    path: str
    current_hash: str
    current_content: str = field(repr=False)
    deletes_created_file: bool = False


@dataclass(frozen=True, slots=True)
class PreparedFileRestore:
    """A multi-file restore candidate tied to one latest task checkpoint."""

    checkpoint_id: str
    files: tuple[PreparedFileRestoreEntry, ...]
    preview: str

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def delete_count(self) -> int:
        return sum(entry.deletes_created_file for entry in self.files)

    @property
    def path(self) -> str:
        """Keep the single-file convenience used by existing integrations."""

        return self.files[0].path

    @property
    def current_hash(self) -> str:
        """Keep the single-file convenience used by existing integrations."""

        return self.files[0].current_hash

    @property
    def deletes_created_file(self) -> bool:
        """Report whether a one-file restore deletes an Agent-created file."""

        return self.file_count == 1 and self.files[0].deletes_created_file


@dataclass(slots=True)
class _ActiveTaskCheckpoint:
    checkpoint_id: str
    edits: dict[str, FileEditCheckpoint]


class FileCheckpointHistory:
    """Keep bounded task-level file checkpoints in the current process."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_FILE_CHECKPOINTS,
        max_content_chars: int = MAX_CHECKPOINT_CONTENT_CHARS,
        max_files_per_checkpoint: int = MAX_FILES_PER_CHECKPOINT,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_entries < 1:
            raise ValueError("checkpoint max_entries must be at least 1")
        if max_content_chars < 1:
            raise ValueError("checkpoint max_content_chars must be at least 1")
        if max_files_per_checkpoint < 1:
            raise ValueError("checkpoint max_files_per_checkpoint must be at least 1")
        self._max_entries = max_entries
        self._max_content_chars = max_content_chars
        self._max_files_per_checkpoint = max_files_per_checkpoint
        self._id_factory = id_factory or (lambda: secrets.token_hex(8))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._items: list[FileTaskCheckpoint] = []
        self._active: _ActiveTaskCheckpoint | None = None

    @property
    def count(self) -> int:
        return len(self._items)

    @property
    def latest(self) -> FileTaskCheckpoint | None:
        return self._items[-1] if self._items else None

    @property
    def active(self) -> bool:
        return self._active is not None

    @property
    def snapshots(self) -> tuple[FileTaskCheckpoint, ...]:
        """Return an immutable view for metadata-only history projection."""

        return tuple(self._items)

    def begin_task(self) -> str:
        """Open one Agent-turn boundary before any file tool executes."""

        if self._active is not None:
            raise ToolError("已有文件检查点任务正在记录，拒绝重叠任务。")
        checkpoint_id = self._id_factory()
        self._active = _ActiveTaskCheckpoint(checkpoint_id, {})
        return checkpoint_id

    def finish_task(self, checkpoint_id: str) -> FileTaskCheckpoint | None:
        """Finalize effective edits even when the surrounding Agent turn failed."""

        active = self._active
        if active is None or active.checkpoint_id != checkpoint_id:
            raise ToolError("文件检查点任务边界已变化。")
        self._active = None
        if not active.edits:
            return None
        checkpoint = FileTaskCheckpoint(
            checkpoint_id=active.checkpoint_id,
            created_at=self._now(),
            edits=tuple(active.edits.values()),
        )
        self._items.append(checkpoint)
        self._trim()
        return checkpoint

    def ensure_capacity(
        self,
        path: str,
        original_content: str | None,
        resulting_content: str,
    ) -> None:
        """Fail before a write that cannot be represented by the active task."""

        if not path or len(path) > MAX_CHECKPOINT_PATH_CHARS:
            raise ToolError(
                f"文件检查点路径最多允许 {MAX_CHECKPOINT_PATH_CHARS} 个字符，"
                "已在写入前拒绝。"
            )
        active = self._active
        existing = active.edits.get(path) if active is not None else None
        active_file_count = len(active.edits) if active is not None else 0
        if existing is not None:
            active_file_count -= 1
        if active_file_count >= self._max_files_per_checkpoint:
            raise ToolError(
                "本次任务修改的文件数量超过检查点上限，已在写入前拒绝；请拆分任务。"
            )
        active_original_chars = (
            self._task_content_chars(active.edits.values()) if active is not None else 0
        )
        if existing is not None and existing.original_content is not None:
            active_original_chars -= len(existing.original_content)
        first_content = (
            existing.original_content if existing is not None else original_content
        )
        added_original_chars = len(first_content) if first_content is not None else 0
        active_result_chars = (
            sum(edit.resulting_chars for edit in active.edits.values())
            if active is not None
            else 0
        )
        if existing is not None:
            active_result_chars -= existing.resulting_chars
        if (
            active_original_chars + added_original_chars > self._max_content_chars
            or active_result_chars + len(resulting_content) > self._max_content_chars
        ):
            raise ToolError(
                "本次任务的文件检查点容量不足，已在写入前拒绝；请拆分任务或使用 Git。"
            )

    def record(
        self,
        path: str,
        original_content: str | None,
        resulting_content: str,
    ) -> FileTaskCheckpoint:
        """Record only after a corresponding atomic write succeeded."""

        self.ensure_capacity(path, original_content, resulting_content)
        owns_boundary = self._active is None
        checkpoint_id = self.begin_task() if owns_boundary else self._active_id()
        active = self._require_active(checkpoint_id)
        previous = active.edits.get(path)
        first_content = (
            previous.original_content if previous is not None else original_content
        )
        if first_content is not None and _content_hash(first_content) == _content_hash(
            resulting_content
        ):
            active.edits.pop(path, None)
        else:
            active.edits[path] = FileEditCheckpoint(
                path=path,
                original_content=first_content,
                resulting_hash=_content_hash(resulting_content),
                resulting_chars=len(resulting_content),
            )
        snapshot = FileTaskCheckpoint(
            checkpoint_id=checkpoint_id,
            created_at=self._now(),
            edits=tuple(active.edits.values()),
        )
        if not owns_boundary:
            return snapshot
        finalized = self.finish_task(checkpoint_id)
        if finalized is None:
            raise ToolError("文件写入没有产生可恢复的内容变化。")
        return finalized

    def consume(
        self,
        checkpoint_id: str,
        current_hashes: Mapping[str, str],
    ) -> FileTaskCheckpoint:
        """Pop the latest task only when every path identity still matches."""

        checkpoint = self.latest
        if checkpoint is None:
            raise ToolError("当前进程没有可恢复的文件任务检查点。")
        if checkpoint.checkpoint_id != checkpoint_id:
            raise ToolError("文件任务检查点已变化，请重新预览。")
        expected_paths = {edit.path for edit in checkpoint.edits}
        if set(current_hashes) != expected_paths:
            raise ToolError("文件任务检查点范围已变化，请重新预览。")
        if any(
            current_hashes[edit.path] != edit.resulting_hash
            for edit in checkpoint.edits
        ):
            raise ToolError("任务文件在 Agent 编辑后发生外部变化，拒绝恢复。")
        self._items.pop()
        return checkpoint

    def _active_id(self) -> str:
        if self._active is None:
            raise ToolError("文件检查点任务尚未开始。")
        return self._active.checkpoint_id

    def _require_active(self, checkpoint_id: str) -> _ActiveTaskCheckpoint:
        active = self._active
        if active is None or active.checkpoint_id != checkpoint_id:
            raise ToolError("文件检查点任务边界已变化。")
        return active

    def _trim(self) -> None:
        while len(self._items) > self._max_entries:
            self._items.pop(0)
        while (
            len(self._items) > 1
            and self._stored_content_chars() > self._max_content_chars
        ):
            self._items.pop(0)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ToolError("文件检查点时间必须包含时区。")
        return value.astimezone(timezone.utc)

    def _stored_content_chars(self) -> int:
        return sum(
            self._task_content_chars(checkpoint.edits) for checkpoint in self._items
        )

    @staticmethod
    def _task_content_chars(edits: Iterable[FileEditCheckpoint]) -> int:
        return sum(
            len(edit.original_content)
            for edit in edits
            if edit.original_content is not None
        )


def content_hash(content: str) -> str:
    """Return the full digest used for external-change detection."""

    return _content_hash(content)


def _content_hash(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()
