"""Tests for Time Machine Phase 3B checkpoint restore gates."""

from __future__ import annotations

from datetime import datetime, timezone

from neil_agent.time_machine import (
    TimeMachineCheckpointPoint,
    TimeMachineSelection,
    TimeMachineSnapshot,
)
from neil_agent.time_machine_restore import (
    can_offer_checkpoint_restore,
    checkpoint_restore_hint,
    latest_checkpoint_id,
)
from neil_agent.projections import (
    ExecutionGraphProjector,
    MetricsProjector,
    TimelineProjector,
)

_NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _snapshot(*checkpoint_ids: str) -> TimeMachineSnapshot:
    timeline = TimelineProjector().project(())
    graph = ExecutionGraphProjector().project(())
    checkpoints = tuple(
        TimeMachineCheckpointPoint(
            checkpoint_id=checkpoint_id,
            created_at=_NOW,
            file_count=1,
            created_file_count=0,
            modified_file_count=1,
            resulting_chars=12,
        )
        for checkpoint_id in checkpoint_ids
    )
    return TimeMachineSnapshot(
        version=1,
        timeline=timeline,
        cursor_sequence=0,
        graph=graph,
        metrics=MetricsProjector().project(graph),
        sessions=(),
        checkpoints=checkpoints,
        input_event_count=0,
        unique_event_count=0,
        event_window_dropped=0,
        session_window_dropped=0,
        projection_anomaly_count=0,
        invalid_session_count=0,
        persistence_enabled=False,
        persistent_event_count=0,
    )


def test_latest_checkpoint_id_returns_newest_point() -> None:
    snapshot = _snapshot("older-checkpoint", "latest-checkpoint")

    assert latest_checkpoint_id(snapshot) == "latest-checkpoint"


def test_restore_gate_requires_latest_selection_and_idle_state() -> None:
    snapshot = _snapshot("older-checkpoint", "latest-checkpoint")
    latest = TimeMachineSelection("checkpoint", "latest-checkpoint")
    older = TimeMachineSelection("checkpoint", "older-checkpoint")

    assert can_offer_checkpoint_restore(
        snapshot,
        latest,
        busy=False,
        pending_approvals=0,
        restore_available=True,
    )
    assert not can_offer_checkpoint_restore(
        snapshot,
        older,
        busy=False,
        pending_approvals=0,
        restore_available=True,
    )
    assert not can_offer_checkpoint_restore(
        snapshot,
        latest,
        busy=True,
        pending_approvals=0,
        restore_available=True,
    )
    assert not can_offer_checkpoint_restore(
        snapshot,
        latest,
        busy=False,
        pending_approvals=1,
        restore_available=True,
    )


def test_restore_hint_explains_git_fallback_for_older_checkpoint() -> None:
    snapshot = _snapshot("older-checkpoint", "latest-checkpoint")
    older = TimeMachineSelection("checkpoint", "older-checkpoint")

    hint = checkpoint_restore_hint(
        snapshot,
        older,
        busy=False,
        pending_approvals=0,
        restore_available=True,
    )

    assert hint is not None
    assert "GIT" in hint
