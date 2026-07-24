"""Tests for deterministic runtime graph, timeline, and plain-text replay."""

from __future__ import annotations

import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

from neil_agent.events import (
    RuntimeEvent,
    RuntimeMetadataItem,
    RuntimeStage,
    RuntimeStatus,
)
from neil_agent.projections import (
    ExecutionGraphProjector,
    TimelineProjector,
    render_runtime_replay,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _event(
    number: int,
    *,
    correlation_id: str,
    stage: RuntimeStage,
    status: RuntimeStatus,
    offset_ms: int,
    parent_event_id: str | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=f"evt-{number:032x}",
        correlation_id=correlation_id,
        parent_event_id=parent_event_id,
        timestamp=NOW + timedelta(milliseconds=offset_ms),
        stage=stage,
        status=status,
    )


def test_fixed_runtime_fixture_has_stable_plain_text_replay() -> None:
    events = tuple(
        RuntimeEvent.model_validate_json(line)
        for line in (FIXTURE_ROOT / "runtime_events_v1.jsonl").read_bytes().splitlines()
    )
    expected = (
        (FIXTURE_ROOT / "runtime_replay_v1.txt")
        .read_text(encoding="utf-8")
        .rstrip("\n")
    )

    assert render_runtime_replay(events) == expected


def test_projection_is_independent_of_arrival_order_and_marks_missing_data() -> None:
    turn_id = "turn-" + "a" * 32
    model_id = "model-" + "b" * 32
    turn_start = _event(
        1,
        correlation_id=turn_id,
        stage="agent_turn",
        status="started",
        offset_ms=0,
    )
    model_finish_without_start = _event(
        2,
        correlation_id=model_id,
        stage="model_request",
        status="succeeded",
        offset_ms=20,
        parent_event_id="evt-" + "f" * 32,
    )
    events = (
        model_finish_without_start,
        turn_start,
        turn_start,
    )

    graph = ExecutionGraphProjector().project(events)
    reversed_graph = ExecutionGraphProjector().project(reversed(events))
    timeline = TimelineProjector().project(events)
    reversed_timeline = TimelineProjector().project(reversed(events))

    assert graph == reversed_graph
    assert timeline == reversed_timeline
    assert graph.input_event_count == 3
    assert graph.unique_event_count == 2
    assert {anomaly.code for anomaly in graph.anomalies} == {
        "duplicate_event",
        "missing_parent_event",
        "missing_start",
    }
    model_node = graph.node(model_id)
    assert model_node is not None
    assert model_node.status == "succeeded"
    assert model_node.started_at is None
    assert model_node.unresolved_parent_event_id == "evt-" + "f" * 32


def test_metadata_order_does_not_create_a_conflicting_event() -> None:
    event = _event(
        3,
        correlation_id="turn-" + "c" * 32,
        stage="agent_turn",
        status="started",
        offset_ms=0,
    ).model_copy(
        update={
            "metadata": (
                RuntimeMetadataItem(name="input_chars", value=12),
                RuntimeMetadataItem(name="history_messages", value=3),
            )
        }
    )
    reordered = event.model_copy(update={"metadata": tuple(reversed(event.metadata))})

    timeline = TimelineProjector().project((event, reordered))
    reversed_timeline = TimelineProjector().project((reordered, event))

    assert timeline == reversed_timeline
    assert timeline.unique_event_count == 1
    assert [anomaly.code for anomaly in timeline.anomalies] == ["duplicate_event"]
    assert [item.name for item in timeline.entries[0].metadata] == [
        "history_messages",
        "input_chars",
    ]


def test_node_state_machine_resolves_conflicts_and_invalid_time() -> None:
    tool_id = "tool-" + "c" * 32
    start = _event(
        10,
        correlation_id=tool_id,
        stage="tool_call",
        status="started",
        offset_ms=30,
    )
    succeeded = _event(
        11,
        correlation_id=tool_id,
        stage="tool_call",
        status="succeeded",
        offset_ms=20,
        parent_event_id=start.event_id,
    )
    failed = _event(
        12,
        correlation_id=tool_id,
        stage="tool_call",
        status="failed",
        offset_ms=10,
        parent_event_id=start.event_id,
    )
    repeated_success = _event(
        13,
        correlation_id=tool_id,
        stage="tool_call",
        status="succeeded",
        offset_ms=25,
        parent_event_id=start.event_id,
    )

    node = (
        ExecutionGraphProjector()
        .project((succeeded, start, failed, repeated_success))
        .nodes[0]
    )

    assert node.status == "failed"
    assert node.finished_at == failed.timestamp
    assert set(node.anomalies) == {
        "conflicting_terminal_status",
        "finish_before_start",
        "repeated_state",
    }


def test_conflicting_event_ids_and_parent_cycles_have_fixed_resolution() -> None:
    turn_id = "turn-" + "d" * 32
    model_id = "model-" + "e" * 32
    turn = _event(
        20,
        correlation_id=turn_id,
        stage="agent_turn",
        status="started",
        offset_ms=0,
        parent_event_id="evt-" + f"{21:032x}",
    )
    conflicting_turn = turn.model_copy(
        update={"timestamp": NOW + timedelta(milliseconds=1)}
    )
    model = _event(
        21,
        correlation_id=model_id,
        stage="model_request",
        status="started",
        offset_ms=0,
        parent_event_id=turn.event_id,
    )

    graph = ExecutionGraphProjector().project((conflicting_turn, model, turn))

    assert graph.unique_event_count == 2
    assert any(
        anomaly.code == "conflicting_event_id" and anomaly.subject_id == turn.event_id
        for anomaly in graph.anomalies
    )
    breaker = graph.node(max(turn_id, model_id))
    assert breaker is not None
    assert breaker.parent_correlation_id is None
    assert "cycle_parent_removed" in breaker.anomalies
    assert len(graph.edges) == 1


def test_replay_limits_large_views_without_losing_totals() -> None:
    events = tuple(
        _event(
            number,
            correlation_id=f"tool-{number:032x}",
            stage="tool_call",
            status="started",
            offset_ms=number,
        )
        for number in range(1, 6)
    )

    output = render_runtime_replay(
        events,
        max_timeline_entries=2,
        max_graph_nodes=2,
    )

    assert "events 5 input / 5 unique | nodes 5" in output
    assert "... 3 timeline entries omitted ..." in output
    assert "... 3 graph nodes omitted ..." in output


def test_ten_thousand_events_have_bounded_stable_projection() -> None:
    events = tuple(
        event
        for node_number in range(1, 5_001)
        for event in (
            _event(
                node_number * 2 - 1,
                correlation_id=f"tool-{node_number:032x}",
                stage="tool_call",
                status="started",
                offset_ms=node_number * 2,
            ),
            _event(
                node_number * 2,
                correlation_id=f"tool-{node_number:032x}",
                stage="tool_call",
                status="succeeded",
                offset_ms=node_number * 2 + 1,
                parent_event_id=f"evt-{node_number * 2 - 1:032x}",
            ),
        )
    )

    tracemalloc.start()
    started_at = perf_counter()
    graph = ExecutionGraphProjector().project(events)
    timeline = TimelineProjector().project(reversed(events))
    elapsed = perf_counter() - started_at
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert graph == ExecutionGraphProjector().project(reversed(events))
    assert graph.input_event_count == 10_000
    assert graph.unique_event_count == 10_000
    assert len(graph.nodes) == 5_000
    assert len(timeline.entries) == 10_000
    assert graph.anomalies == ()
    assert timeline.anomalies == ()
    assert elapsed < 10
    assert peak < 128 * 1024 * 1024
