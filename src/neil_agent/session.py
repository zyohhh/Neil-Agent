"""Versioned, workspace-local conversation snapshots."""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Self
from unicodedata import category

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .errors import SessionError
from .schemas import Message, validate_message_history
from .task import (
    MAX_QUALITY_OUTPUT_CHARS,
    MAX_TASK_STEP_CHARS,
    MAX_TASK_STEPS,
    PlanStepStatus,
    QualityCheckRecord,
    QualityCheckStatus,
    TaskStep,
    TaskTracker,
)

LEGACY_SESSION_FORMAT_VERSION: Literal[1] = 1
SESSION_FORMAT_VERSION: Literal[2] = 2
SESSION_STATE_DIRECTORY = ".neil-agent"
SESSION_DIRECTORY = "sessions"
MAX_SESSION_FILE_BYTES = 25_000_000
MAX_LISTED_SESSIONS = 50
MAX_SESSION_TITLE_CHARS = 80
MAX_SESSION_QUERY_CHARS = 80
UNTITLED_SESSION = "新会话"
SESSION_ID_PATTERN_TEXT = r"^\d{8}T\d{12}Z-[0-9a-f]{8}$"
SESSION_ID_PATTERN = re.compile(SESSION_ID_PATTERN_TEXT)


class StoredTaskStep(BaseModel):
    """Serializable task step stored in a session snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TASK_STEP_CHARS)
    status: PlanStepStatus

    @classmethod
    def from_task_step(cls, step: TaskStep) -> StoredTaskStep:
        return cls(title=step.title, status=step.status)

    def to_task_step(self) -> TaskStep:
        return TaskStep(title=self.title, status=self.status)


class StoredQualityCheck(BaseModel):
    """Serializable latest quality-check record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: str = Field(min_length=1, max_length=80)
    status: QualityCheckStatus
    command: str | None = Field(default=None, max_length=2_000)
    exit_code: int | None = None
    output: str = Field(max_length=MAX_QUALITY_OUTPUT_CHARS + 100)

    @classmethod
    def from_record(cls, record: QualityCheckRecord) -> StoredQualityCheck:
        return cls(
            check=record.check,
            status=record.status,
            command=record.command,
            exit_code=record.exit_code,
            output=record.output,
        )

    def to_record(self) -> QualityCheckRecord:
        return QualityCheckRecord(
            check=self.check,
            status=self.status,
            command=self.command,
            exit_code=self.exit_code,
            output=self.output,
        )


class SessionState(BaseModel):
    """Fields shared by supported local session versions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(pattern=SESSION_ID_PATTERN_TEXT)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    messages: tuple[Message, ...] = ()
    plan: tuple[StoredTaskStep, ...] = Field(default=(), max_length=MAX_TASK_STEPS)
    latest_quality_check: StoredQualityCheck | None = None

    @model_validator(mode="after")
    def validate_snapshot_state(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        validate_message_history(self.messages)
        TaskTracker().restore(
            tuple(step.to_task_step() for step in self.plan),
            (
                self.latest_quality_check.to_record()
                if self.latest_quality_check is not None
                else None
            ),
        )
        return self

    def restored_steps(self) -> tuple[TaskStep, ...]:
        return tuple(step.to_task_step() for step in self.plan)

    def restored_quality_check(self) -> QualityCheckRecord | None:
        if self.latest_quality_check is None:
            return None
        return self.latest_quality_check.to_record()


class SessionSnapshotV1(SessionState):
    """Legacy snapshot retained for strict, read-only migration."""

    version: Literal[1] = LEGACY_SESSION_FORMAT_VERSION

    def migrate(self) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=self.session_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
            title=_default_session_title(self.messages),
            messages=self.messages,
            plan=self.plan,
            latest_quality_check=self.latest_quality_check,
        )


class SessionSnapshot(SessionState):
    """Current version of one complete, resumable local session."""

    version: Literal[2] = SESSION_FORMAT_VERSION
    title: str = Field(min_length=1, max_length=MAX_SESSION_TITLE_CHARS)

    @field_validator("title")
    @classmethod
    def title_must_be_safe(cls, value: str) -> str:
        return normalize_session_title(value)


SESSION_SNAPSHOT_ADAPTER: TypeAdapter[SessionSnapshot | SessionSnapshotV1] = (
    TypeAdapter(SessionSnapshot | SessionSnapshotV1)
)


@dataclass(frozen=True, slots=True)
class SessionHandle:
    """Identity and original creation time for the active session."""

    session_id: str
    created_at: datetime
    title: str = ""


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Small session record displayed by ``/sessions``."""

    session_id: str
    title: str
    updated_at: datetime
    round_count: int
    size_bytes: int
    preview: str


@dataclass(frozen=True, slots=True)
class SessionIndex:
    """Valid session summaries plus the number of unreadable files."""

    sessions: tuple[SessionSummary, ...]
    valid_count: int = 0
    matched_count: int = 0
    invalid_count: int = 0
    total_size_bytes: int = 0


class SessionStore:
    """Atomically save and explicitly load workspace-local sessions."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"workspace root is not a directory: {root}")
        self._workspace_root = root
        self._root = root / SESSION_STATE_DIRECTORY / SESSION_DIRECTORY
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: secrets.token_hex(4))

    @property
    def root(self) -> Path:
        return self._root

    def new_session(self) -> SessionHandle:
        """Create an in-memory identity without writing an empty snapshot."""

        created_at = self._now()
        suffix = self._id_factory()
        session_id = f"{created_at:%Y%m%dT%H%M%S%fZ}-{suffix}"
        self._validate_session_id(session_id)
        return SessionHandle(session_id=session_id, created_at=created_at)

    @staticmethod
    def handle_for(snapshot: SessionSnapshot) -> SessionHandle:
        return SessionHandle(
            session_id=snapshot.session_id,
            created_at=snapshot.created_at,
            title=snapshot.title,
        )

    def save(
        self,
        handle: SessionHandle,
        messages: Sequence[Message],
        steps: Sequence[TaskStep],
        latest_quality_check: QualityCheckRecord | None,
    ) -> SessionSnapshot:
        """Atomically replace one versioned snapshot."""

        self._validate_session_id(handle.session_id)
        created_at = self._normalize_time(handle.created_at)
        updated_at = max(self._now(), created_at)
        try:
            title = (
                normalize_session_title(handle.title)
                if handle.title
                else _default_session_title(messages)
            )
            snapshot = SessionSnapshot(
                session_id=handle.session_id,
                created_at=created_at,
                updated_at=updated_at,
                title=title,
                messages=tuple(messages),
                plan=tuple(StoredTaskStep.from_task_step(step) for step in steps),
                latest_quality_check=(
                    StoredQualityCheck.from_record(latest_quality_check)
                    if latest_quality_check is not None
                    else None
                ),
            )
        except ValueError as error:
            raise SessionError(f"无法保存无效会话：{error}") from error

        self._write_snapshot(snapshot)
        return snapshot

    def rename(self, session_id: str, title: str) -> SessionSnapshot:
        """Atomically rename one saved session without changing its contents."""

        normalized = normalize_session_title(title)
        snapshot = self.load(session_id)
        renamed = SessionSnapshot(
            session_id=snapshot.session_id,
            created_at=snapshot.created_at,
            updated_at=max(self._now(), snapshot.updated_at),
            title=normalized,
            messages=snapshot.messages,
            plan=snapshot.plan,
            latest_quality_check=snapshot.latest_quality_check,
        )
        self._write_snapshot(renamed)
        return renamed

    def has_saved(self, session_id: str) -> bool:
        """Return whether any exact session path already exists."""

        self._validate_session_id(session_id)
        root = self._resolved_root(create=False)
        if root is None:
            return False
        try:
            (root / f"{session_id}.json").lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise SessionError("无法检查本地会话文件。") from error
        return True

    def load(self, session_id: str) -> SessionSnapshot:
        """Load one explicitly selected session by its exact ID."""

        self._validate_session_id(session_id)
        root = self._resolved_root(create=False)
        if root is None:
            raise SessionError("尚无已保存的本地会话。")
        path = root / f"{session_id}.json"
        if not path.exists():
            raise SessionError(f"未找到本地会话：{session_id}")
        return self._load_path(path, expected_id=session_id)

    def get_summary(self, session_id: str) -> SessionSummary:
        """Return a safe preview for one exact session before an action."""

        snapshot = self.load(session_id)
        path = self._root / f"{session_id}.json"
        try:
            size_bytes = path.stat().st_size
        except OSError as error:
            raise SessionError(f"无法读取本地会话大小：{error}") from error
        return self._summary(snapshot, size_bytes=size_bytes)

    def delete(self, session_id: str) -> SessionSummary:
        """Delete one exact, valid session after the caller obtains approval."""

        summary = self.get_summary(session_id)
        path = self._root / f"{session_id}.json"
        try:
            path.unlink()
        except OSError as error:
            raise SessionError(f"删除本地会话失败：{error}") from error
        return summary

    def list_sessions(self, query: str = "") -> SessionIndex:
        """Return newest valid snapshots without failing on one corrupt file."""

        normalized_query = normalize_session_query(query)
        root = self._resolved_root(create=False)
        if root is None:
            return SessionIndex(())
        summaries: list[SessionSummary] = []
        invalid_count = 0
        total_size_bytes = 0
        try:
            paths = tuple(root.glob("*.json"))
        except OSError as error:
            raise SessionError(f"无法列出本地会话：{error}") from error
        for path in paths:
            try:
                size_bytes = path.lstat().st_size
                total_size_bytes += size_bytes
            except OSError:
                invalid_count += 1
                continue
            try:
                snapshot = self._load_path(path, expected_id=path.stem)
            except SessionError:
                invalid_count += 1
                continue
            summary = self._summary(snapshot, size_bytes=size_bytes)
            if not normalized_query or _summary_matches(summary, normalized_query):
                summaries.append(summary)
        summaries.sort(key=lambda item: item.updated_at, reverse=True)
        return SessionIndex(
            sessions=tuple(summaries[:MAX_LISTED_SESSIONS]),
            valid_count=len(paths) - invalid_count,
            matched_count=len(summaries),
            invalid_count=invalid_count,
            total_size_bytes=total_size_bytes,
        )

    def _load_path(self, path: Path, *, expected_id: str) -> SessionSnapshot:
        try:
            root = self._resolved_root(create=False)
            if root is None:
                raise SessionError("尚无已保存的本地会话。")
            resolved = path.resolve(strict=True)
            if resolved.parent != root or resolved != path:
                raise SessionError("拒绝读取会话目录之外的文件。")
            if resolved.stat().st_size > MAX_SESSION_FILE_BYTES:
                raise SessionError("会话快照过大，拒绝读取。")
            payload = resolved.read_bytes()
            parsed = SESSION_SNAPSHOT_ADAPTER.validate_json(payload)
            snapshot = (
                parsed.migrate() if isinstance(parsed, SessionSnapshotV1) else parsed
            )
        except SessionError:
            raise
        except (OSError, ValueError) as error:
            raise SessionError(f"本地会话无效或无法读取：{path.name}") from error
        if snapshot.session_id != expected_id:
            raise SessionError("会话文件名与内部 ID 不一致。")
        return snapshot

    def _write_snapshot(self, snapshot: SessionSnapshot) -> None:
        payload = (snapshot.model_dump_json(indent=2) + "\n").encode("utf-8")
        if len(payload) > MAX_SESSION_FILE_BYTES:
            raise SessionError(
                f"会话快照超过 {MAX_SESSION_FILE_BYTES} 字节，未执行保存。"
            )

        root = self._resolved_root(create=True)
        assert root is not None
        target = root / f"{snapshot.session_id}.json"
        descriptor = -1
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{snapshot.session_id}.",
                suffix=".tmp",
                dir=root,
            )
            temporary_path = Path(temporary_name)
            output = os.fdopen(descriptor, "wb")
            descriptor = -1
            with output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, target)
        except OSError as error:
            raise SessionError(f"本地会话保存失败：{error}") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)
            if temporary_path is not None and temporary_path.exists():
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _resolved_root(self, *, create: bool) -> Path | None:
        try:
            if create:
                self._root.mkdir(parents=True, exist_ok=True)
            elif not self._root.exists():
                return None
            resolved = self._root.resolve(strict=True)
        except OSError as error:
            raise SessionError(f"无法访问本地会话目录：{error}") from error
        if resolved != self._root or not resolved.is_dir():
            raise SessionError("本地会话目录必须是工作区内的真实目录。")
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as error:
            raise SessionError("本地会话目录越过工作区边界。") from error
        return resolved

    def _now(self) -> datetime:
        return self._normalize_time(self._clock())

    @staticmethod
    def _normalize_time(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise SessionError("会话时间必须包含时区。")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise SessionError("会话 ID 格式无效。")

    @staticmethod
    def _summary(
        snapshot: SessionSnapshot,
        *,
        size_bytes: int,
    ) -> SessionSummary:
        user_messages = [
            message.content
            for message in snapshot.messages
            if message.role == "user" and not message.tool_results
        ]
        preview = _single_line(user_messages[-1] if user_messages else "（空会话）")
        return SessionSummary(
            session_id=snapshot.session_id,
            title=snapshot.title,
            updated_at=snapshot.updated_at,
            round_count=len(user_messages),
            size_bytes=size_bytes,
            preview=preview,
        )


def normalize_session_title(value: str) -> str:
    """Validate and normalize one human-entered local session title."""

    title = value.strip()
    if not title:
        raise SessionError("会话标题不能为空。")
    if len(title) > MAX_SESSION_TITLE_CHARS:
        raise SessionError(f"会话标题最多 {MAX_SESSION_TITLE_CHARS} 个字符。")
    if any(category(character).startswith("C") for character in title):
        raise SessionError("会话标题不能包含控制或格式字符。")
    return title


def normalize_session_query(value: str) -> str:
    """Validate a bounded, single-line local session search query."""

    query = value.strip()
    if len(query) > MAX_SESSION_QUERY_CHARS:
        raise SessionError(f"会话搜索最多 {MAX_SESSION_QUERY_CHARS} 个字符。")
    if any(category(character).startswith("C") for character in query):
        raise SessionError("会话搜索不能包含控制或格式字符。")
    return query.casefold()


def _default_session_title(messages: Sequence[Message]) -> str:
    first_request = next(
        (
            message.content
            for message in messages
            if message.role == "user" and not message.tool_results
        ),
        UNTITLED_SESSION,
    )
    return _single_line(first_request, max_chars=MAX_SESSION_TITLE_CHARS)


def _summary_matches(summary: SessionSummary, query: str) -> bool:
    return any(
        query in value.casefold()
        for value in (summary.session_id, summary.title, summary.preview)
    )


def _single_line(value: str, max_chars: int = 80) -> str:
    safe_value = "".join(
        " " if category(character).startswith("C") else character for character in value
    )
    text = " ".join(safe_value.split()) or "（空会话）"
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."
