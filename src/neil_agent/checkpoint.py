"""In-memory checkpoints for edits performed by Neil Agent file tools."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256

from .errors import ToolError

MAX_FILE_CHECKPOINTS = 20
MAX_CHECKPOINT_CONTENT_CHARS = 5_000_000


@dataclass(frozen=True, slots=True)
class FileEditCheckpoint:
    """Original content and post-edit identity for one successful tool write."""

    checkpoint_id: str
    path: str
    resulting_hash: str
    original_content: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class PreparedFileRestore:
    """An approved restore candidate tied to exact current file content."""

    checkpoint_id: str
    path: str
    current_hash: str
    preview: str
    deletes_created_file: bool = False


class FileCheckpointHistory:
    """Keep a bounded stack of Agent-owned file edits in the current process."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_FILE_CHECKPOINTS,
        max_content_chars: int = MAX_CHECKPOINT_CONTENT_CHARS,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_entries < 1:
            raise ValueError("checkpoint max_entries must be at least 1")
        if max_content_chars < 1:
            raise ValueError("checkpoint max_content_chars must be at least 1")
        self._max_entries = max_entries
        self._max_content_chars = max_content_chars
        self._id_factory = id_factory or (lambda: secrets.token_hex(8))
        self._items: list[FileEditCheckpoint] = []

    @property
    def count(self) -> int:
        return len(self._items)

    @property
    def latest(self) -> FileEditCheckpoint | None:
        return self._items[-1] if self._items else None

    def record(
        self,
        path: str,
        original_content: str | None,
        resulting_content: str,
    ) -> FileEditCheckpoint:
        """Record only after the corresponding atomic write succeeded."""

        checkpoint = FileEditCheckpoint(
            checkpoint_id=self._id_factory(),
            path=path,
            original_content=original_content,
            resulting_hash=_content_hash(resulting_content),
        )
        self._items.append(checkpoint)
        self._trim()
        return checkpoint

    def consume(self, checkpoint_id: str, current_hash: str) -> FileEditCheckpoint:
        """Pop the latest checkpoint only when identity and content still match."""

        checkpoint = self.latest
        if checkpoint is None:
            raise ToolError("当前进程没有可恢复的文件编辑检查点。")
        if checkpoint.checkpoint_id != checkpoint_id:
            raise ToolError("文件检查点已变化，请重新预览。")
        if checkpoint.resulting_hash != current_hash:
            raise ToolError("文件在 Agent 编辑后发生外部变化，拒绝恢复。")
        self._items.pop()
        return checkpoint

    def _trim(self) -> None:
        while len(self._items) > self._max_entries:
            self._items.pop(0)
        while (
            len(self._items) > 1
            and self._stored_content_chars() > self._max_content_chars
        ):
            self._items.pop(0)

    def _stored_content_chars(self) -> int:
        return sum(
            len(item.original_content)
            for item in self._items
            if item.original_content is not None
        )


def content_hash(content: str) -> str:
    """Return the full digest used for external-change detection."""

    return _content_hash(content)


def _content_hash(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()
