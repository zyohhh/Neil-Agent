"""Tests for the metadata-only Time Machine historical projection."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from neil_agent.checkpoint import FileEditCheckpoint, FileTaskCheckpoint
from neil_agent.events import RuntimeEvent, RuntimeMetadataItem, RuntimeStatus
from neil_agent.session import SessionSummary
from neil_agent.time_machine import (
    MAX_TIME_MACHINE_EVENTS,
    TimeMachineHistory,
    TimeMachineHistoryProjection,
    TimeMachineProjector,
    TimeMachineSelection,
    render_time_machine_snapshot,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _event(
    number: int,
    *,
    correlation_id: str,
    status: RuntimeStatus,
    parent_event_id: str | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=f"evt-{number:032x}",
        correlation_id=correlation_id,
        parent_event_id=parent_event_id,
        timestamp=NOW + timedelta(milliseconds=number),
        stage="agent_turn",
        status=status,
    )


def _runtime_events() -> tuple[RuntimeEvent, ...]:
    correlation_id = "turn-" + "a" * 32
    started = _event(1, correlation_id=correlation_id, status="started")
    finished = _event(
        2,
        correlation_id=correlation_id,
        status="succeeded",
        parent_event_id=started.event_id,
    ).model_copy(
        update={
            "metadata": (
                RuntimeMetadataItem(name="model_requests", value=1),
                RuntimeMetadataItem(name="tool_calls", value=0),
                RuntimeMetadataItem(name="response_chars", value=12),
                RuntimeMetadataItem(name="elapsed_ms", value=8),
            )
        }
    )
    return started, finished


def _history() -> TimeMachineHistory:
    root_id = "20260823T120000000000Z-aaaaaaaa"
    branch_id = "20260823T120001000000Z-bbbbbbbb"
    sessions = (
        SessionSummary(
            session_id=root_id,
            title="SESSION-TITLE-CANARY",
            created_at=NOW,
            updated_at=NOW,
            round_count=2,
            size_bytes=100,
            preview="SESSION-PREVIEW-CANARY",
            has_plan=True,
            has_compaction=True,
        ),
        SessionSummary(
            session_id=branch_id,
            title="BRANCH-TITLE-CANARY",
            created_at=NOW + timedelta(seconds=1),
            updated_at=NOW + timedelta(seconds=2),
            round_count=2,
            size_bytes=100,
            preview="BRANCH-PREVIEW-CANARY",
            parent_session_id=root_id,
        ),
    )
    checkpoints = (
        FileTaskCheckpoint(
            checkpoint_id="checkpoint-1",
            created_at=NOW + timedelta(seconds=3),
            edits=(
                FileEditCheckpoint(
                    path="PRIVATE-PATH-CANARY.txt",
                    original_content="PRIVATE-ORIGINAL-CANARY",
                    resulting_hash="PRIVATE-HASH-CANARY",
                    resulting_chars=20,
                ),
                FileEditCheckpoint(
                    path="PRIVATE-CREATED-PATH-CANARY.txt",
                    original_content=None,
                    resulting_hash="PRIVATE-CREATED-HASH-CANARY",
                    resulting_chars=10,
                ),
            ),
        ),
    )
    return TimeMachineHistory(sessions=sessions, checkpoints=checkpoints)


def test_time_machine_rebuilds_deterministic_state_at_event_cursor() -> None:
    projector = TimeMachineProjector()
    events = _runtime_events()

    started = projector.project(reversed(events), cursor_sequence=1)
    finished = projector.project(events)

    assert started.version == 1
    assert started.cursor_sequence == 1
    assert started.graph.nodes[0].status == "started"
    assert started.metrics.active_nodes == 1
    assert finished.cursor_sequence == 2
    assert finished.graph.nodes[0].status == "succeeded"
    assert finished.metrics.succeeded_nodes == 1
    assert finished.timeline.entries == started.timeline.entries


def test_time_machine_projects_lineage_compaction_and_checkpoint_counts_only() -> None:
    snapshot = TimeMachineProjector().project(_runtime_events(), _history())
    root, branch = snapshot.sessions
    checkpoint = snapshot.checkpoints[0]

    assert root.lineage == "root"
    assert root.has_compaction is True
    assert branch.lineage == "branch"
    assert branch.parent_session_id == root.session_id
    assert checkpoint.file_count == 2
    assert checkpoint.created_file_count == 1
    assert checkpoint.modified_file_count == 1
    assert checkpoint.resulting_chars == 30

    rendered = "\n".join(
        (
            render_time_machine_snapshot(
                snapshot,
                TimeMachineSelection("session", branch.session_id),
            ),
            render_time_machine_snapshot(
                snapshot,
                TimeMachineSelection("checkpoint", checkpoint.checkpoint_id),
            ),
            repr(snapshot),
        )
    )
    for canary in (
        "SESSION-TITLE-CANARY",
        "SESSION-PREVIEW-CANARY",
        "BRANCH-TITLE-CANARY",
        "BRANCH-PREVIEW-CANARY",
        "PRIVATE-PATH-CANARY",
        "PRIVATE-ORIGINAL-CANARY",
        "PRIVATE-HASH-CANARY",
    ):
        assert canary not in rendered
    assert "MESSAGE BODIES HIDDEN" in rendered
    assert "PATHS, HASHES, AND FILE BODIES HIDDEN" in rendered


def test_time_machine_bounds_event_window_and_rejects_invalid_cursor() -> None:
    events = tuple(
        _event(
            number,
            correlation_id=f"turn-{number:032x}",
            status="started",
        )
        for number in range(1, MAX_TIME_MACHINE_EVENTS + 9)
    )
    projector = TimeMachineProjector()

    snapshot = projector.project(events)

    assert len(snapshot.timeline.entries) == MAX_TIME_MACHINE_EVENTS
    assert snapshot.event_window_dropped == 8
    assert snapshot.input_event_count == MAX_TIME_MACHINE_EVENTS + 8
    with pytest.raises(ValueError, match="cursor"):
        projector.project(events, cursor_sequence=MAX_TIME_MACHINE_EVENTS + 1)


def test_time_machine_history_rejects_unbounded_or_invalid_sources() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        TimeMachineHistory(invalid_session_count=-1)
    with pytest.raises(ValueError, match="session source"):
        TimeMachineHistory(sessions=_history().sessions * 26)

    with pytest.raises(ValueError, match="counts"):
        TimeMachineHistoryProjection(version=1, invalid_session_count=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone"):
        TimeMachineHistory(
            sessions=(
                replace(
                    _history().sessions[0],
                    created_at=datetime(2026, 8, 23, 12, 0),
                ),
            )
        )


def test_time_machine_rejects_boolean_cursor_and_non_integer_persistent_count() -> None:
    projector = TimeMachineProjector()

    with pytest.raises(ValueError, match="cursor"):
        projector.project(_runtime_events(), cursor_sequence=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="persistent event count"):
        projector.project(_runtime_events(), persistent_event_count="1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="selection kind"):
        TimeMachineSelection("restore", "checkpoint-1")  # type: ignore[arg-type]
