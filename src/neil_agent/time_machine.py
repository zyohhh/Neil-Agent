"""Bounded, metadata-only Time Machine projections for historical browsing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from unicodedata import category

from .checkpoint import MAX_FILES_PER_CHECKPOINT, FileTaskCheckpoint
from .events import RuntimeEvent
from .projections import (
    ExecutionGraph,
    ExecutionGraphProjector,
    MetricsProjector,
    RuntimeMetrics,
    RuntimeTimeline,
    TimelineEntry,
    TimelineProjector,
)
from .session import SessionSummary

TIME_MACHINE_VERSION: Literal[1] = 1
MAX_TIME_MACHINE_EVENTS = 512
MAX_TIME_MACHINE_SESSIONS = 50
MAX_TIME_MACHINE_CHECKPOINTS = 20

SessionLineageState = Literal["root", "branch", "orphaned_branch"]
TimeMachineSelectionKind = Literal["event", "session", "checkpoint"]


@dataclass(frozen=True, slots=True)
class TimeMachineSessionPoint:
    """One content-free session state available to the history browser."""

    session_id: str
    parent_session_id: str | None
    lineage: SessionLineageState
    created_at: datetime
    updated_at: datetime
    round_count: int
    has_plan: bool
    failed_check: bool
    has_compaction: bool


@dataclass(frozen=True, slots=True)
class TimeMachineCheckpointPoint:
    """One task checkpoint summary without paths, hashes, or file bodies."""

    checkpoint_id: str
    created_at: datetime
    file_count: int
    created_file_count: int
    modified_file_count: int
    resulting_chars: int


@dataclass(frozen=True, slots=True)
class TimeMachineHistory:
    """Bounded source facts supplied by a host without mutation callbacks."""

    sessions: tuple[SessionSummary, ...] = ()
    checkpoints: tuple[FileTaskCheckpoint, ...] = ()
    invalid_session_count: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.invalid_session_count) is not int
            or self.invalid_session_count < 0
        ):
            raise ValueError("invalid session count cannot be negative")
        if len(self.sessions) > MAX_TIME_MACHINE_SESSIONS:
            raise ValueError("time machine session source exceeds its bound")
        if len(self.checkpoints) > MAX_TIME_MACHINE_CHECKPOINTS:
            raise ValueError("time machine checkpoint source exceeds its bound")
        if any(not isinstance(item, SessionSummary) for item in self.sessions):
            raise ValueError("time machine session source contains an invalid item")
        if any(not isinstance(item, FileTaskCheckpoint) for item in self.checkpoints):
            raise ValueError("time machine checkpoint source contains an invalid item")
        for session in self.sessions:
            _require_aware(session.created_at, "session created time")
            _require_aware(session.updated_at, "session updated time")
            _require_identifier(session.session_id, "session ID")
            if session.parent_session_id is not None:
                _require_identifier(session.parent_session_id, "parent session ID")
            if session.updated_at < session.created_at:
                raise ValueError("time machine session update precedes creation")
            if type(session.round_count) is not int or session.round_count < 0:
                raise ValueError("time machine session round count is invalid")
            if any(
                type(value) is not bool
                for value in (
                    session.has_plan,
                    session.failed_check,
                    session.has_compaction,
                )
            ):
                raise ValueError("time machine session flags must be boolean")
        for checkpoint in self.checkpoints:
            _require_aware(checkpoint.created_at, "checkpoint created time")
            _require_identifier(checkpoint.checkpoint_id, "checkpoint ID")
            if checkpoint.file_count > MAX_FILES_PER_CHECKPOINT:
                raise ValueError("time machine checkpoint file count exceeds its bound")
            if any(
                type(edit.resulting_chars) is not int or edit.resulting_chars < 0
                for edit in checkpoint.edits
            ):
                raise ValueError("time machine checkpoint character count is invalid")


@dataclass(frozen=True, slots=True)
class TimeMachineHistoryProjection:
    """Sanitized history facts safe for a long-lived UI to retain."""

    version: Literal[1] = TIME_MACHINE_VERSION
    sessions: tuple[TimeMachineSessionPoint, ...] = ()
    checkpoints: tuple[TimeMachineCheckpointPoint, ...] = ()
    invalid_session_count: int = 0
    session_window_dropped: int = 0

    def __post_init__(self) -> None:
        if self.version != TIME_MACHINE_VERSION:
            raise ValueError("unsupported time machine history version")
        if len(self.sessions) > MAX_TIME_MACHINE_SESSIONS:
            raise ValueError("time machine session projection exceeds its bound")
        if len(self.checkpoints) > MAX_TIME_MACHINE_CHECKPOINTS:
            raise ValueError("time machine checkpoint projection exceeds its bound")
        for value in (self.invalid_session_count, self.session_window_dropped):
            if type(value) is not int or value < 0:
                raise ValueError("time machine history counts cannot be negative")
        if any(not isinstance(item, TimeMachineSessionPoint) for item in self.sessions):
            raise ValueError("time machine session projection contains an invalid item")
        if any(
            not isinstance(item, TimeMachineCheckpointPoint)
            for item in self.checkpoints
        ):
            raise ValueError(
                "time machine checkpoint projection contains an invalid item"
            )


@dataclass(frozen=True, slots=True)
class TimeMachineSelection:
    """Stable UI selection that cannot carry source content."""

    kind: TimeMachineSelectionKind
    key: str

    def __post_init__(self) -> None:
        if self.kind not in {"event", "session", "checkpoint"}:
            raise ValueError("time machine selection kind is invalid")
        _require_identifier(self.key, "selection key")


@dataclass(frozen=True, slots=True)
class TimeMachineSnapshot:
    """Runtime as-of projection plus bounded current local-history metadata."""

    version: Literal[1]
    timeline: RuntimeTimeline
    cursor_sequence: int
    graph: ExecutionGraph
    metrics: RuntimeMetrics
    sessions: tuple[TimeMachineSessionPoint, ...]
    checkpoints: tuple[TimeMachineCheckpointPoint, ...]
    input_event_count: int
    unique_event_count: int
    event_window_dropped: int
    session_window_dropped: int
    projection_anomaly_count: int
    invalid_session_count: int
    persistence_enabled: bool
    persistent_event_count: int

    @property
    def selected_event(self) -> TimelineEntry | None:
        if self.cursor_sequence == 0:
            return None
        return self.timeline.entries[self.cursor_sequence - 1]

    def session(self, session_id: str) -> TimeMachineSessionPoint | None:
        return next(
            (item for item in self.sessions if item.session_id == session_id),
            None,
        )

    def checkpoint(self, checkpoint_id: str) -> TimeMachineCheckpointPoint | None:
        return next(
            (item for item in self.checkpoints if item.checkpoint_id == checkpoint_id),
            None,
        )


class TimeMachineProjector:
    """Rebuild historical state exclusively from already-recorded safe facts."""

    def project(
        self,
        events: Iterable[RuntimeEvent],
        history: TimeMachineHistory | TimeMachineHistoryProjection = (
            TimeMachineHistoryProjection()
        ),
        *,
        cursor_sequence: int | None = None,
        persistence_enabled: bool = False,
        persistent_event_count: int = 0,
    ) -> TimeMachineSnapshot:
        if type(persistence_enabled) is not bool:
            raise ValueError("time machine persistence flag must be boolean")
        if type(persistent_event_count) is not int or persistent_event_count < 0:
            raise ValueError("persistent event count cannot be negative")

        full_timeline = TimelineProjector().project(events)
        visible_entries = full_timeline.entries[-MAX_TIME_MACHINE_EVENTS:]
        visible_events = tuple(_event_from_entry(entry) for entry in visible_entries)
        timeline = TimelineProjector().project(visible_events)
        cursor = len(timeline.entries) if cursor_sequence is None else cursor_sequence
        if type(cursor) is not int or not 0 <= cursor <= len(timeline.entries):
            raise ValueError("time machine cursor is outside the visible event window")

        projected_events = tuple(
            _event_from_entry(entry) for entry in timeline.entries[:cursor]
        )
        graph = ExecutionGraphProjector().project(projected_events)
        if isinstance(history, TimeMachineHistory):
            projected_history = self.sanitize_history(history)
        elif isinstance(history, TimeMachineHistoryProjection):
            projected_history = history
        else:
            raise TypeError("time machine history source is invalid")
        return TimeMachineSnapshot(
            version=TIME_MACHINE_VERSION,
            timeline=timeline,
            cursor_sequence=cursor,
            graph=graph,
            metrics=MetricsProjector().project(graph),
            sessions=projected_history.sessions,
            checkpoints=projected_history.checkpoints,
            input_event_count=full_timeline.input_event_count,
            unique_event_count=full_timeline.unique_event_count,
            event_window_dropped=(
                full_timeline.unique_event_count - len(timeline.entries)
            ),
            session_window_dropped=projected_history.session_window_dropped,
            projection_anomaly_count=len(full_timeline.anomalies),
            invalid_session_count=projected_history.invalid_session_count,
            persistence_enabled=persistence_enabled,
            persistent_event_count=min(
                persistent_event_count,
                full_timeline.input_event_count,
            ),
        )

    def sanitize_history(
        self,
        history: TimeMachineHistory,
    ) -> TimeMachineHistoryProjection:
        """Irreversibly remove session text and checkpoint file identities."""

        if not isinstance(history, TimeMachineHistory):
            raise TypeError("time machine history sanitizer requires source history")
        session_points, dropped_sessions = _project_sessions(history.sessions)
        return TimeMachineHistoryProjection(
            sessions=session_points,
            checkpoints=_project_checkpoints(history.checkpoints),
            invalid_session_count=history.invalid_session_count,
            session_window_dropped=dropped_sessions,
        )


def render_time_machine_snapshot(
    snapshot: TimeMachineSnapshot,
    selection: TimeMachineSelection | None = None,
) -> str:
    """Render a dependency-free replay summary without source content."""

    if not isinstance(snapshot, TimeMachineSnapshot):
        raise TypeError("time machine renderer requires a TimeMachineSnapshot")
    persistence = "ON" if snapshot.persistence_enabled else "MEMORY ONLY"
    lines = [
        f"NEIL TIME MACHINE v{snapshot.version} · READ ONLY",
        (
            f"events {len(snapshot.timeline.entries)}/{snapshot.unique_event_count} "
            f"· cursor {snapshot.cursor_sequence} · store {persistence} "
            f"· loaded {snapshot.persistent_event_count}"
        ),
        (
            f"current sessions {len(snapshot.sessions)} · checkpoints "
            f"{len(snapshot.checkpoints)} · invalid sessions "
            f"{snapshot.invalid_session_count}"
        ),
    ]
    if selection is None:
        selection = (
            TimeMachineSelection("event", snapshot.selected_event.event_id)
            if snapshot.selected_event is not None
            else None
        )
    lines.extend(("", _render_selection(snapshot, selection)))
    return "\n".join(lines)


def _render_selection(
    snapshot: TimeMachineSnapshot,
    selection: TimeMachineSelection | None,
) -> str:
    if selection is None:
        return "NO HISTORICAL FACTS"
    if selection.kind == "event":
        entry = next(
            (
                candidate
                for candidate in snapshot.timeline.entries
                if candidate.event_id == selection.key
            ),
            None,
        )
        if entry is None:
            return "EVENT NOT AVAILABLE"
        sequence = entry.sequence
        metadata = " ".join(f"{item.name}={item.value}" for item in entry.metadata)
        suffix = f" · {metadata}" if metadata else ""
        return (
            f"EVENT {sequence:04d} · {_timestamp(entry.timestamp)} · "
            f"{entry.stage.upper()} {entry.status.upper()}{suffix}\n"
            f"AS-OF NODES {snapshot.metrics.total_nodes} · "
            f"ACTIVE {snapshot.metrics.active_nodes} · "
            f"FAILED {snapshot.metrics.failed_nodes} · "
            f"TOKENS {snapshot.metrics.input_tokens}/"
            f"{snapshot.metrics.output_tokens}"
        )
    if selection.kind == "session":
        session_point = snapshot.session(selection.key)
        if session_point is None:
            return "SESSION NOT AVAILABLE"
        flags = [session_point.lineage.upper()]
        if session_point.has_compaction:
            flags.append("COMPACTED")
        if session_point.has_plan:
            flags.append("PLAN")
        if session_point.failed_check:
            flags.append("CHECK FAILED")
        return (
            f"SESSION {_short_session_id(session_point.session_id)} · "
            f"{' · '.join(flags)}\n"
            f"CREATED {_timestamp(session_point.created_at)} · UPDATED "
            f"{_timestamp(session_point.updated_at)} · "
            f"ROUNDS {session_point.round_count}\n"
            f"PARENT {_short_session_id(session_point.parent_session_id)} · "
            "MESSAGE BODIES HIDDEN"
        )
    checkpoint_point = snapshot.checkpoint(selection.key)
    if checkpoint_point is None:
        return "CHECKPOINT NOT AVAILABLE"
    return (
        f"TASK CHECKPOINT {_short_checkpoint_id(checkpoint_point.checkpoint_id)} · "
        f"{_timestamp(checkpoint_point.created_at)}\n"
        f"FILES {checkpoint_point.file_count} · "
        f"CREATED {checkpoint_point.created_file_count} · "
        f"MODIFIED {checkpoint_point.modified_file_count} · "
        f"RESULT {checkpoint_point.resulting_chars} CHARS\n"
        "PATHS, HASHES, AND FILE BODIES HIDDEN"
    )


def _project_sessions(
    sessions: tuple[SessionSummary, ...],
) -> tuple[tuple[TimeMachineSessionPoint, ...], int]:
    ordered = sorted(
        sessions,
        key=lambda item: (
            item.created_at.astimezone(timezone.utc),
            item.updated_at.astimezone(timezone.utc),
            item.session_id,
        ),
    )
    dropped = max(len(ordered) - MAX_TIME_MACHINE_SESSIONS, 0)
    selected = ordered[-MAX_TIME_MACHINE_SESSIONS:]
    selected_ids = {item.session_id for item in selected}
    points = tuple(
        TimeMachineSessionPoint(
            session_id=item.session_id,
            parent_session_id=item.parent_session_id,
            lineage=(
                "root"
                if item.parent_session_id is None
                else "branch"
                if item.parent_session_id in selected_ids
                else "orphaned_branch"
            ),
            created_at=item.created_at.astimezone(timezone.utc),
            updated_at=item.updated_at.astimezone(timezone.utc),
            round_count=item.round_count,
            has_plan=item.has_plan,
            failed_check=item.failed_check,
            has_compaction=item.has_compaction,
        )
        for item in selected
    )
    return points, dropped


def _project_checkpoints(
    checkpoints: tuple[FileTaskCheckpoint, ...],
) -> tuple[TimeMachineCheckpointPoint, ...]:
    ordered = sorted(
        checkpoints,
        key=lambda item: (
            item.created_at.astimezone(timezone.utc),
            item.checkpoint_id,
        ),
    )[-MAX_TIME_MACHINE_CHECKPOINTS:]
    return tuple(
        TimeMachineCheckpointPoint(
            checkpoint_id=item.checkpoint_id,
            created_at=item.created_at.astimezone(timezone.utc),
            file_count=item.file_count,
            created_file_count=sum(
                edit.original_content is None for edit in item.edits
            ),
            modified_file_count=sum(
                edit.original_content is not None for edit in item.edits
            ),
            resulting_chars=sum(edit.resulting_chars for edit in item.edits),
        )
        for item in ordered
    )


def _event_from_entry(entry: TimelineEntry) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=entry.event_id,
        correlation_id=entry.correlation_id,
        parent_event_id=entry.parent_event_id,
        timestamp=entry.timestamp,
        stage=entry.stage,
        status=entry.status,
        metadata=entry.metadata,
    )


def _timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _short_session_id(value: str | None) -> str:
    if value is None:
        return "-"
    return value[-8:]


def _short_checkpoint_id(value: str) -> str:
    return value[:12]


def _require_aware(value: datetime, label: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"time machine {label} must include a timezone")


def _require_identifier(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(category(character).startswith("C") for character in value)
    ):
        raise ValueError(f"time machine {label} is invalid")
