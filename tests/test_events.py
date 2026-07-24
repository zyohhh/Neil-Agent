"""Tests for metadata-only runtime events and observer isolation."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Event

import pytest
from pydantic import ValidationError

from neil_agent.events import (
    EventBus,
    RuntimeEvent,
    RuntimeEventEmitter,
    RuntimeEventFactory,
    RuntimeMetadataItem,
    redact_runtime_metadata,
)


class TokenSequence:
    def __init__(self, start: int = 0) -> None:
        self._value = start

    def __call__(self) -> str:
        self._value += 1
        return f"{self._value:032x}"


def _factory(*, token_start: int = 0) -> RuntimeEventFactory:
    return RuntimeEventFactory(
        clock=lambda: datetime(2026, 7, 24, 1, 2, 3, tzinfo=timezone.utc),
        token_factory=TokenSequence(token_start),
    )


def _event(*, token_start: int = 0) -> RuntimeEvent:
    factory = _factory(token_start=token_start)
    return factory.create(
        stage="agent_turn",
        status="started",
        correlation_id=factory.new_correlation_id("agent_turn"),
        metadata={
            "input_chars": 12,
            "history_messages": 0,
            "history_rounds": 0,
        },
    )


def test_runtime_event_is_versioned_correlated_and_immutable() -> None:
    factory = _factory()
    correlation_id = factory.new_correlation_id("model_request")
    started = factory.create(
        stage="model_request",
        status="started",
        correlation_id=correlation_id,
        metadata={"model_round": 1, "message_count": 3, "tool_count": 5},
    )
    finished = factory.create(
        stage="model_request",
        status="succeeded",
        correlation_id=correlation_id,
        parent_event_id=started.event_id,
        metadata={"text_chars": 48, "tool_calls": 0, "elapsed_ms": 12},
    )

    assert started.version == 1
    assert started.timestamp == datetime(
        2026,
        7,
        24,
        1,
        2,
        3,
        tzinfo=timezone.utc,
    )
    assert started.event_id != finished.event_id
    assert finished.correlation_id == started.correlation_id
    assert finished.parent_event_id == started.event_id
    assert started.metadata_dict() == {
        "model_round": 1,
        "message_count": 3,
        "tool_count": 5,
    }
    with pytest.raises(ValidationError, match="frozen"):
        started.status = "failed"  # type: ignore[misc]


def test_runtime_event_rejects_unknown_fields_and_invalid_stage_contracts() -> None:
    factory = _factory()
    correlation_id = factory.new_correlation_id("approval")

    with pytest.raises(ValidationError, match="Extra inputs"):
        RuntimeEvent(
            event_id="evt-" + "1" * 32,
            correlation_id=correlation_id,
            timestamp=datetime.now(timezone.utc),
            stage="approval",
            status="waiting",
            unexpected=True,  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError, match="not valid for its stage"):
        factory.create(
            stage="approval",
            status="started",
            correlation_id=correlation_id,
        )
    with pytest.raises(ValidationError, match="does not match its stage"):
        factory.create(
            stage="approval",
            status="waiting",
            correlation_id="tool-" + "2" * 32,
        )


def test_runtime_metadata_redactor_rejects_content_and_control_characters() -> None:
    with pytest.raises(ValueError, match="unknown runtime metadata"):
        redact_runtime_metadata("agent_turn", {"prompt": "secret"})
    with pytest.raises(ValidationError, match="control or format"):
        redact_runtime_metadata("tool_call", {"tool_name": "read_file\x1b[31m"})
    with pytest.raises(ValidationError, match="exceeds 200"):
        redact_runtime_metadata("model_request", {"error_type": "x" * 201})
    with pytest.raises(ValidationError):
        RuntimeMetadataItem(name="elapsed_ms", value=-1)


def test_event_emitter_reuses_span_identity_for_state_changes() -> None:
    events: list[RuntimeEvent] = []
    bus = EventBus()
    bus.subscribe(events.append)
    emitter = RuntimeEventEmitter(bus, factory=_factory())

    span = emitter.start(
        "tool_call",
        metadata={
            "tool_name": "read_file",
            "argument_count": 1,
            "requires_approval": False,
        },
    )
    emitter.finish(
        span,
        "succeeded",
        metadata={"is_error": False, "result_chars": 120, "elapsed_ms": 4},
    )

    assert bus.flush()
    assert [event.status for event in events] == ["started", "succeeded"]
    assert events[0].correlation_id == events[1].correlation_id
    assert events[1].parent_event_id == events[0].event_id
    assert bus.close()


def test_event_bus_isolates_failing_observers_and_preserves_order() -> None:
    delivered: list[str] = []
    bus = EventBus(queue_size=4)

    def fail(_event: RuntimeEvent) -> None:
        raise RuntimeError("observer failure")

    bus.subscribe(fail)
    bus.subscribe(lambda event: delivered.append(event.event_id))
    first = _event()
    second = _event(token_start=10)

    assert bus.publish(first).accepted_deliveries == 2
    assert bus.publish(second).accepted_deliveries == 2
    assert bus.flush()

    stats = bus.stats
    assert delivered == [first.event_id, second.event_id]
    assert stats.published_events == 2
    assert stats.delivered_deliveries == 2
    assert stats.observer_failures == 2
    assert stats.dropped_deliveries == 0
    assert bus.close()


def test_event_bus_drops_only_observations_when_a_queue_is_full() -> None:
    entered = Event()
    release = Event()
    bus = EventBus(queue_size=1)

    def slow(_event: RuntimeEvent) -> None:
        entered.set()
        release.wait(2)

    bus.subscribe(slow)
    first = _event()
    second = _event(token_start=10)
    third = _event(token_start=20)

    assert bus.publish(first).accepted_deliveries == 1
    assert entered.wait(1)
    assert bus.publish(second).accepted_deliveries == 1
    result = bus.publish(third)

    assert result.accepted_deliveries == 0
    assert result.dropped_deliveries == 1
    assert bus.stats.dropped_deliveries == 1
    release.set()
    assert bus.flush()
    assert bus.stats.delivered_deliveries == 2
    assert bus.close()


def test_event_bus_enforces_observer_and_lifecycle_bounds() -> None:
    bus = EventBus(queue_size=1, max_observers=1)
    subscription = bus.subscribe(lambda _event: None)

    with pytest.raises(ValueError, match="at most 1"):
        bus.subscribe(lambda _event: None)

    subscription.close()
    assert bus.stats.observer_count == 0
    assert bus.close()
    assert bus.stats.closed
    with pytest.raises(RuntimeError, match="closed"):
        bus.subscribe(lambda _event: None)
