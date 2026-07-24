"""Versioned, metadata-only runtime events and a bounded observer bus."""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from queue import Empty, Full, Queue
from threading import Condition, Event, Thread
from time import monotonic
from typing import Literal, TypeAlias
from unicodedata import category

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

RUNTIME_EVENT_VERSION: Literal[1] = 1
MAX_RUNTIME_METADATA_FIELDS = 16
MAX_RUNTIME_METADATA_TEXT_CHARS = 200
MAX_RUNTIME_METADATA_INTEGER = 2**63 - 1
DEFAULT_EVENT_QUEUE_SIZE = 256
DEFAULT_MAX_EVENT_OBSERVERS = 8

RuntimeStage = Literal[
    "agent_turn",
    "model_request",
    "tool_call",
    "approval",
    "quality_check",
]
RuntimeStatus = Literal["started", "waiting", "succeeded", "skipped", "failed"]
RuntimeMetadataName = Literal[
    "argument_count",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "elapsed_ms",
    "error_type",
    "history_messages",
    "history_rounds",
    "input_chars",
    "input_tokens",
    "is_error",
    "message_count",
    "model_round",
    "model_requests",
    "omitted_rounds",
    "output_tokens",
    "preview_chars",
    "requires_approval",
    "response_chars",
    "result_chars",
    "selected_messages",
    "text_chars",
    "thinking_blocks",
    "tool_calls",
    "tool_count",
    "tool_name",
]
RuntimeMetadataValue: TypeAlias = StrictBool | StrictInt | StrictStr
RuntimeObserver = Callable[["RuntimeEvent"], None]
RuntimeTokenFactory = Callable[[], str]
RuntimeClock = Callable[[], datetime]

_EVENT_ID_PATTERN_TEXT = r"^evt-[0-9a-f]{32}$"
_CORRELATION_ID_PATTERN_TEXT = r"^(turn|model|tool|approval|check)-[0-9a-f]{32}$"
_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_CORRELATION_PREFIX: dict[RuntimeStage, str] = {
    "agent_turn": "turn",
    "model_request": "model",
    "tool_call": "tool",
    "approval": "approval",
    "quality_check": "check",
}
_STAGE_STATUSES: dict[RuntimeStage, frozenset[RuntimeStatus]] = {
    "agent_turn": frozenset({"started", "succeeded", "skipped", "failed"}),
    "model_request": frozenset({"started", "succeeded", "failed"}),
    "tool_call": frozenset({"started", "succeeded", "skipped", "failed"}),
    "approval": frozenset({"waiting", "succeeded", "skipped", "failed"}),
    "quality_check": frozenset({"started", "succeeded", "skipped", "failed"}),
}
_STAGE_METADATA_FIELDS: dict[
    RuntimeStage,
    tuple[RuntimeMetadataName, ...],
] = {
    "agent_turn": (
        "input_chars",
        "history_messages",
        "history_rounds",
        "selected_messages",
        "omitted_rounds",
        "model_requests",
        "tool_calls",
        "response_chars",
        "elapsed_ms",
        "error_type",
    ),
    "model_request": (
        "model_round",
        "message_count",
        "tool_count",
        "text_chars",
        "thinking_blocks",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "elapsed_ms",
        "error_type",
    ),
    "tool_call": (
        "tool_name",
        "argument_count",
        "requires_approval",
        "is_error",
        "result_chars",
        "elapsed_ms",
        "error_type",
    ),
    "approval": (
        "tool_name",
        "preview_chars",
        "elapsed_ms",
        "error_type",
    ),
    "quality_check": (
        "is_error",
        "result_chars",
        "elapsed_ms",
        "error_type",
    ),
}


class RuntimeMetadataItem(BaseModel):
    """One immutable, allowlisted metadata field."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: RuntimeMetadataName
    value: RuntimeMetadataValue

    @model_validator(mode="after")
    def validate_bounded_value(self) -> RuntimeMetadataItem:
        value = self.value
        if isinstance(value, str):
            if not value:
                raise ValueError("runtime metadata text cannot be empty")
            if len(value) > MAX_RUNTIME_METADATA_TEXT_CHARS:
                raise ValueError(
                    "runtime metadata text exceeds "
                    f"{MAX_RUNTIME_METADATA_TEXT_CHARS} characters"
                )
            if any(category(character).startswith("C") for character in value):
                raise ValueError(
                    "runtime metadata text contains a control or format character"
                )
        elif not isinstance(value, bool) and (
            value < 0 or value > MAX_RUNTIME_METADATA_INTEGER
        ):
            raise ValueError("runtime metadata integer is outside the safe range")
        return self


class RuntimeEvent(BaseModel):
    """One immutable fact emitted by the Agent runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    version: Literal[1] = RUNTIME_EVENT_VERSION
    event_id: str = Field(pattern=_EVENT_ID_PATTERN_TEXT)
    correlation_id: str = Field(pattern=_CORRELATION_ID_PATTERN_TEXT)
    parent_event_id: str | None = Field(
        default=None,
        pattern=_EVENT_ID_PATTERN_TEXT,
    )
    timestamp: AwareDatetime
    stage: RuntimeStage
    status: RuntimeStatus
    metadata: tuple[RuntimeMetadataItem, ...] = Field(
        default=(),
        max_length=MAX_RUNTIME_METADATA_FIELDS,
    )

    @model_validator(mode="after")
    def validate_event_contract(self) -> RuntimeEvent:
        expected_prefix = _CORRELATION_PREFIX[self.stage]
        if not self.correlation_id.startswith(f"{expected_prefix}-"):
            raise ValueError("runtime correlation ID does not match its stage")
        if self.status not in _STAGE_STATUSES[self.stage]:
            raise ValueError("runtime status is not valid for its stage")
        names = tuple(item.name for item in self.metadata)
        if len(set(names)) != len(names):
            raise ValueError("runtime metadata contains duplicate fields")
        allowed = frozenset(_STAGE_METADATA_FIELDS[self.stage])
        unknown = set(names) - allowed
        if unknown:
            raise ValueError(
                "runtime metadata contains fields not allowed for its stage: "
                + ", ".join(sorted(unknown))
            )
        return self

    def metadata_dict(self) -> dict[str, bool | int | str]:
        """Return a detached mapping suitable for projectors and tests."""

        return {item.name: item.value for item in self.metadata}


def redact_runtime_metadata(
    stage: RuntimeStage,
    values: Mapping[str, object] | None = None,
) -> tuple[RuntimeMetadataItem, ...]:
    """Keep only an explicit metadata schema and reject unsafe values.

    The function deliberately has no generic string fallback: callers cannot
    accidentally pass prompts, tool arguments, results, previews, or project
    instruction text through an unknown field.
    """

    if values is None:
        return ()
    if not isinstance(values, Mapping):
        raise ValueError("runtime metadata must be a mapping")
    allowed = _STAGE_METADATA_FIELDS[stage]
    unknown = set(values) - set(allowed)
    if unknown:
        raise ValueError(
            "unknown runtime metadata fields: "
            + ", ".join(sorted(str(name) for name in unknown))
        )
    return tuple(
        RuntimeMetadataItem(name=name, value=values[name])  # type: ignore[arg-type]
        for name in allowed
        if name in values
    )


class RuntimeEventFactory:
    """Create validated events and stable per-operation correlation IDs."""

    def __init__(
        self,
        *,
        clock: RuntimeClock | None = None,
        token_factory: RuntimeTokenFactory | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._token_factory = token_factory or (lambda: secrets.token_hex(16))

    def new_correlation_id(self, stage: RuntimeStage) -> str:
        """Allocate one ID reused by every state event for an operation."""

        return f"{_CORRELATION_PREFIX[stage]}-{self._token()}"

    def create(
        self,
        *,
        stage: RuntimeStage,
        status: RuntimeStatus,
        correlation_id: str,
        parent_event_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> RuntimeEvent:
        """Create one unique, versioned runtime fact."""

        return RuntimeEvent(
            event_id=f"evt-{self._token()}",
            correlation_id=correlation_id,
            parent_event_id=parent_event_id,
            timestamp=self._clock(),
            stage=stage,
            status=status,
            metadata=redact_runtime_metadata(stage, metadata),
        )

    def _token(self) -> str:
        token = self._token_factory()
        if not isinstance(token, str) or _TOKEN_PATTERN.fullmatch(token) is None:
            raise ValueError("runtime ID token must contain exactly 32 lowercase hex")
        return token


@dataclass(frozen=True, slots=True)
class RuntimeSpan:
    """Stable identity of one started runtime operation."""

    stage: RuntimeStage
    correlation_id: str
    start_event_id: str


@dataclass(frozen=True, slots=True)
class EventPublishResult:
    """Non-blocking delivery outcome for one published event."""

    accepted_deliveries: int
    dropped_deliveries: int


@dataclass(frozen=True, slots=True)
class EventBusStats:
    """Bounded observer-bus counters without event content."""

    published_events: int
    accepted_deliveries: int
    delivered_deliveries: int
    dropped_deliveries: int
    observer_failures: int
    pending_deliveries: int
    observer_count: int
    closed: bool


@dataclass(slots=True)
class _ObserverState:
    observer_id: int
    observer: RuntimeObserver
    queue: Queue[RuntimeEvent]
    stop: Event = field(default_factory=Event)
    thread: Thread | None = None


class EventSubscription:
    """One idempotently removable observer registration."""

    def __init__(self, bus: EventBus, observer_id: int) -> None:
        self._bus = bus
        self._observer_id = observer_id
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._unsubscribe(self._observer_id)

    def __enter__(self) -> EventSubscription:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class EventBus:
    """Publish events without waiting for or trusting observers."""

    def __init__(
        self,
        *,
        queue_size: int = DEFAULT_EVENT_QUEUE_SIZE,
        max_observers: int = DEFAULT_MAX_EVENT_OBSERVERS,
    ) -> None:
        if queue_size < 1:
            raise ValueError("event queue size must be at least 1")
        if max_observers < 1:
            raise ValueError("maximum event observers must be at least 1")
        self._queue_size = queue_size
        self._max_observers = max_observers
        self._condition = Condition()
        self._observers: dict[int, _ObserverState] = {}
        self._next_observer_id = 1
        self._published_events = 0
        self._accepted_deliveries = 0
        self._delivered_deliveries = 0
        self._dropped_deliveries = 0
        self._observer_failures = 0
        self._pending_deliveries = 0
        self._closed = False

    def subscribe(self, observer: RuntimeObserver) -> EventSubscription:
        """Start an isolated daemon worker for one observer."""

        if not callable(observer):
            raise ValueError("runtime event observer must be callable")
        with self._condition:
            if self._closed:
                raise RuntimeError("runtime event bus is closed")
            if len(self._observers) >= self._max_observers:
                raise ValueError(
                    f"runtime event bus allows at most {self._max_observers} observers"
                )
            observer_id = self._next_observer_id
            self._next_observer_id += 1
            state = _ObserverState(
                observer_id=observer_id,
                observer=observer,
                queue=Queue(maxsize=self._queue_size),
            )
            thread = Thread(
                target=self._observe,
                args=(state,),
                name=f"neil-runtime-observer-{observer_id}",
                daemon=True,
            )
            state.thread = thread
            self._observers[observer_id] = state
            thread.start()
        return EventSubscription(self, observer_id)

    def publish(self, event: RuntimeEvent) -> EventPublishResult:
        """Queue one event for every active observer without blocking."""

        if not isinstance(event, RuntimeEvent):
            raise TypeError("event bus accepts only RuntimeEvent instances")
        accepted = 0
        dropped = 0
        with self._condition:
            if self._closed:
                return EventPublishResult(0, 0)
            self._published_events += 1
            for state in self._observers.values():
                if state.stop.is_set():
                    continue
                try:
                    state.queue.put_nowait(event)
                except Full:
                    dropped += 1
                    self._dropped_deliveries += 1
                else:
                    accepted += 1
                    self._accepted_deliveries += 1
                    self._pending_deliveries += 1
            self._condition.notify_all()
        return EventPublishResult(accepted, dropped)

    @property
    def stats(self) -> EventBusStats:
        """Return an immutable counter snapshot."""

        with self._condition:
            return EventBusStats(
                published_events=self._published_events,
                accepted_deliveries=self._accepted_deliveries,
                delivered_deliveries=self._delivered_deliveries,
                dropped_deliveries=self._dropped_deliveries,
                observer_failures=self._observer_failures,
                pending_deliveries=self._pending_deliveries,
                observer_count=len(self._observers),
                closed=self._closed,
            )

    def flush(self, timeout: float = 1.0) -> bool:
        """Wait only when explicitly requested until accepted deliveries finish."""

        if timeout < 0:
            raise ValueError("event bus flush timeout cannot be negative")
        deadline = monotonic() + timeout
        with self._condition:
            while self._pending_deliveries:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self, timeout: float = 1.0) -> bool:
        """Stop observers within a bounded wait and discard queued events."""

        if timeout < 0:
            raise ValueError("event bus close timeout cannot be negative")
        with self._condition:
            if self._closed:
                return self._pending_deliveries == 0
            self._closed = True
            observer_ids = tuple(self._observers)
        states = [
            state
            for observer_id in observer_ids
            if (state := self._unsubscribe(observer_id)) is not None
        ]
        deadline = monotonic() + timeout
        for state in states:
            thread = state.thread
            if thread is None:
                continue
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        with self._condition:
            return self._pending_deliveries == 0

    def __enter__(self) -> EventBus:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _unsubscribe(self, observer_id: int) -> _ObserverState | None:
        with self._condition:
            state = self._observers.pop(observer_id, None)
            if state is None:
                return None
            state.stop.set()
            discarded = 0
            while True:
                try:
                    state.queue.get_nowait()
                except Empty:
                    break
                else:
                    state.queue.task_done()
                    discarded += 1
            self._pending_deliveries -= discarded
            self._dropped_deliveries += discarded
            self._condition.notify_all()
            return state

    def _observe(self, state: _ObserverState) -> None:
        while not state.stop.is_set():
            try:
                event = state.queue.get(timeout=0.05)
            except Empty:
                continue
            failed = False
            discarded = state.stop.is_set()
            if not discarded:
                try:
                    state.observer(event)
                except BaseException:  # noqa: BLE001 - observer isolation boundary.
                    failed = True
            state.queue.task_done()
            with self._condition:
                self._pending_deliveries -= 1
                if discarded:
                    self._dropped_deliveries += 1
                elif failed:
                    self._observer_failures += 1
                else:
                    self._delivered_deliveries += 1
                self._condition.notify_all()


class RuntimeEventEmitter:
    """Create correlated events and publish them through one bus."""

    def __init__(
        self,
        bus: EventBus,
        *,
        factory: RuntimeEventFactory | None = None,
    ) -> None:
        self._bus = bus
        self._factory = factory or RuntimeEventFactory()

    def start(
        self,
        stage: RuntimeStage,
        *,
        parent_event_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
        status: RuntimeStatus = "started",
    ) -> RuntimeSpan:
        correlation_id = self._factory.new_correlation_id(stage)
        event = self._factory.create(
            stage=stage,
            status=status,
            correlation_id=correlation_id,
            parent_event_id=parent_event_id,
            metadata=metadata,
        )
        self._bus.publish(event)
        return RuntimeSpan(
            stage=stage,
            correlation_id=correlation_id,
            start_event_id=event.event_id,
        )

    def finish(
        self,
        span: RuntimeSpan,
        status: RuntimeStatus,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> RuntimeEvent:
        event = self._factory.create(
            stage=span.stage,
            status=status,
            correlation_id=span.correlation_id,
            parent_event_id=span.start_event_id,
            metadata=metadata,
        )
        self._bus.publish(event)
        return event
