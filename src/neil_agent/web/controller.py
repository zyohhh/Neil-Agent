"""Single-run controller, approvals, and bounded realtime transport."""

from __future__ import annotations

import asyncio
import secrets
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Condition, Event, RLock, Thread
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from ..agent import Agent
from ..config import Settings
from ..errors import SessionError
from ..events import EventBus, RuntimeEvent
from ..host_runtime import HostMode, build_host_runtime
from ..providers.factory import create_provider
from ..schemas import ActivityEvent, Message, TokenUsage, ToolCall
from ..session import SessionSnapshot
from ..task import QualityCheckRecord, TaskStep
from .dto import (
    ApprovalRequestDto,
    ContextDto,
    OutputEntryDto,
    QualityCheckDto,
    ReviewState,
    RunDto,
    RuntimeCapabilitiesDto,
    RuntimeStepDto,
    SessionDto,
    SessionListDto,
    TaskDto,
    TaskStepDto,
    WorkbenchSnapshotDto,
)
from .service import WorkbenchSnapshotService

MAX_PROMPT_CHARS = 8_000
MAX_EVENT_HISTORY = 512
MAX_OUTPUT_ENTRIES = 200
MAX_RUNTIME_STEPS = 200
MAX_SUBSCRIBERS = 8
SUBSCRIBER_QUEUE_SIZE = 64
PROTOCOL_VERSION = 1
APPROVAL_TTL = timedelta(minutes=5)


class TurnCancelled(Exception):
    """Internal cooperative cancellation signal with no user content."""


TextSink = Callable[[str], None]
ActivitySink = Callable[[ActivityEvent], None]
RuntimeSink = Callable[[RuntimeEvent], None]
ApprovalSink = Callable[[ToolCall, str], bool]


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Persisted conversation state restored into one Agent turn."""

    messages: tuple[Message, ...] = ()
    last_usage: TokenUsage | None = None
    steps: tuple[TaskStep, ...] = ()
    latest_quality_check: QualityCheckRecord | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Successful Agent history that the controller may persist."""

    messages: tuple[Message, ...]
    last_usage: TokenUsage | None = None
    steps: tuple[TaskStep, ...] = ()
    latest_quality_check: QualityCheckRecord | None = None


class TurnWorker(Protocol):
    """Synchronous worker boundary executed on one daemon thread."""

    def run(
        self,
        prompt: str,
        cancel: Event,
        on_text: TextSink,
        on_activity: ActivitySink,
        on_runtime: RuntimeSink,
        request_approval: ApprovalSink,
        context: TurnContext | None = None,
    ) -> TurnResult | None: ...


class AgentTurnWorker:
    """Run the configured Agent with preview-gated mutation tools for P3."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def run(
        self,
        prompt: str,
        cancel: Event,
        on_text: TextSink,
        on_activity: ActivitySink,
        on_runtime: RuntimeSink,
        request_approval: ApprovalSink,
        context: TurnContext | None = None,
    ) -> TurnResult:
        settings = self._settings
        host_runtime = build_host_runtime(settings, mode=HostMode.WEB)
        registry = host_runtime.registry
        instruction_manager = host_runtime.instruction_manager
        hooks = host_runtime.hooks
        task_tracker = host_runtime.task_tracker
        if task_tracker is None:
            raise RuntimeError("Web host runtime must include a task tracker.")
        restored = context or TurnContext()
        if restored.steps or restored.latest_quality_check is not None:
            task_tracker.restore(restored.steps, restored.latest_quality_check)
        bus = EventBus(queue_size=256, max_observers=1)
        subscription = bus.subscribe(on_runtime)
        model = create_provider(settings, retry_handler=on_activity)
        agent = Agent(
            model,
            system_prompt=settings.system_prompt,
            project_instructions=instruction_manager.current.prompt_section(),
            max_rounds=settings.max_rounds,
            max_context_chars=settings.max_context_chars,
            max_context_tokens=settings.max_context_tokens,
            registry=registry,
            max_tool_rounds=settings.max_tool_rounds,
            activity_handler=on_activity,
            instruction_scope_handler=instruction_manager.resolve_tool_call,
            task_tracker=task_tracker,
            event_bus=bus,
            file_checkpoints=host_runtime.filesystem.checkpoints,
            approval_handler=request_approval,
            hooks=hooks,
        )
        if restored.messages:
            agent.restore_messages(restored.messages, restored.last_usage)
        stream = agent.stream_chat(prompt)
        try:
            for chunk in stream:
                if cancel.is_set():
                    stream.close()
                    raise TurnCancelled
                on_text(chunk)
            if cancel.is_set():
                raise TurnCancelled
            bus.flush(timeout=0.5)
        finally:
            subscription.close()
            bus.close(timeout=0.5)
        return TurnResult(
            messages=agent.messages,
            last_usage=agent.last_usage,
            steps=task_tracker.steps,
            latest_quality_check=task_tracker.latest_quality_check,
        )


CommandName = Literal[
    "acquire_control",
    "release_control",
    "start_turn",
    "cancel_turn",
    "approve_tool",
    "reject_tool",
    "select_session",
    "new_session",
    "ping",
]


class ClientCommand(BaseModel):
    """Strict client command envelope."""

    model_config = ConfigDict(extra="forbid", strict=True)

    protocol_version: Literal[1]
    message_type: Literal["command"]
    command_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")
    expected_revision: int = Field(ge=0)
    command: CommandName
    payload: dict[str, Any] = Field(default_factory=dict, max_length=8)


class CommandError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class ControllerSubscription:
    client_id: str
    queue: asyncio.Queue[dict[str, Any]]
    invalidated: bool = False


@dataclass(slots=True)
class PendingApproval:
    request: ApprovalRequestDto
    decision: bool | None = None


class WorkbenchController:
    """Own one active turn, one control lease, and one replayable event log."""

    def __init__(
        self,
        service: WorkbenchSnapshotService,
        worker: TurnWorker,
        *,
        clock: Callable[[], datetime] | None = None,
        event_history_size: int = MAX_EVENT_HISTORY,
        subscriber_queue_size: int = SUBSCRIBER_QUEUE_SIZE,
    ) -> None:
        if event_history_size < 4 or subscriber_queue_size < 1:
            raise ValueError("controller buffer sizes are too small")
        self._service = service
        self._worker = worker
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self._events: deque[dict[str, Any]] = deque(maxlen=event_history_size)
        self._output: deque[OutputEntryDto] = deque(maxlen=MAX_OUTPUT_ENTRIES)
        self._quality_checks: deque[QualityCheckDto] = deque(maxlen=20)
        self._steps: dict[str, RuntimeStepDto] = {}
        self._subscribers: dict[
            str, tuple[asyncio.AbstractEventLoop, ControllerSubscription]
        ] = {}
        self._subscriber_queue_size = subscriber_queue_size
        self._sequence = 0
        self._revision = 0
        self._control_client_id: str | None = None
        self._run = RunDto()
        self._cancel: Event | None = None
        self._approval_condition = Condition(self._lock)
        self._approval: PendingApproval | None = None
        self._closed = False
        self._command_results: dict[str, dict[str, Any]] = {}
        self._session_store = service.session_store
        self._session_handle = self._session_store.new_session()
        self._session_messages: tuple[Message, ...] = ()
        self._session_usage: TokenUsage | None = None
        self._session_steps: tuple[TaskStep, ...] = ()
        self._session_quality: QualityCheckRecord | None = None
        self._resume_latest_session()

    @classmethod
    def production(
        cls, settings: Settings, service: WorkbenchSnapshotService
    ) -> WorkbenchController:
        return cls(service, AgentTurnWorker(settings))

    def snapshot(self) -> WorkbenchSnapshotDto:
        base = self._service.snapshot()
        with self._lock:
            running = self._run.status in {"running", "cancelling"}
            approval = None if self._approval is None else self._approval.request
            quality_checks = tuple(
                self._quality_checks
            ) or self._restored_quality_checks(base.review.quality_checks)
            latest_quality = quality_checks[-1] if quality_checks else None
            return base.model_copy(
                update={
                    "run": self._run,
                    "revision": self._revision,
                    "last_sequence": self._sequence,
                    "capabilities": RuntimeCapabilitiesDto(
                        can_start_turn=not running and not self._closed,
                        can_cancel_turn=running and not self._closed,
                        can_select_session=not running and not self._closed,
                        can_approve_tool=(
                            approval is not None
                            and approval.state == "pending"
                            and self._control_client_id is not None
                        ),
                        can_show_diff=base.git.available,
                        can_estimate_cost=base.review.cost_available,
                    ),
                    "timeline": tuple(self._steps.values())[-MAX_RUNTIME_STEPS:],
                    "output": tuple(self._output),
                    "approval": approval,
                    "sessions": self._sessions_overlay(base.sessions),
                    "task": self._task_dto(),
                    "context": self._context_dto(),
                    "review": base.review.model_copy(
                        update={
                            "state": self._review_state(base.review.state, approval),
                            "approval_available": (
                                approval is not None and approval.state == "pending"
                            ),
                            "quality_check": latest_quality,
                            "quality_checks": quality_checks,
                        }
                    ),
                }
            )

    def subscribe(self, client_id: str, after: int) -> ControllerSubscription:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._closed:
                raise CommandError("service_closing", "Workbench is shutting down")
            if len(self._subscribers) >= MAX_SUBSCRIBERS:
                raise CommandError("too_many_clients", "Too many local observers")
            subscription = ControllerSubscription(
                client_id=client_id,
                queue=asyncio.Queue(maxsize=self._subscriber_queue_size),
            )
            earliest = (
                self._events[0]["sequence"] if self._events else self._sequence + 1
            )
            if after > self._sequence or after < earliest - 1:
                subscription.queue.put_nowait(self._invalidation("sequence_gap"))
                subscription.invalidated = True
            else:
                replay = [event for event in self._events if event["sequence"] > after]
                if len(replay) > self._subscriber_queue_size:
                    subscription.queue.put_nowait(self._invalidation("replay_overflow"))
                    subscription.invalidated = True
                else:
                    for event in replay:
                        subscription.queue.put_nowait(event)
            self._subscribers[client_id] = (loop, subscription)
            return subscription

    def unsubscribe(self, client_id: str) -> None:
        released = False
        with self._lock:
            self._subscribers.pop(client_id, None)
            if self._control_client_id == client_id:
                self._control_client_id = None
                self._reject_pending_locked("Control client disconnected")
                released = True
        if released:
            self._publish("control_changed", {"holder": None})

    def connected_message(self, client_id: str, after: int) -> dict[str, Any]:
        with self._lock:
            return {
                "protocol_version": PROTOCOL_VERSION,
                "message_type": "connected",
                "client_id": client_id,
                "sequence": after,
                "revision": self._revision,
                "control": self._control_client_id == client_id,
            }

    def handle_command(self, client_id: str, command: ClientCommand) -> dict[str, Any]:
        with self._lock:
            cached = self._command_results.get(command.command_id)
            if cached is not None:
                return cached
        try:
            if command.command == "ping":
                result = self._result(command, "accepted", "pong")
            elif command.command == "acquire_control":
                result = self._acquire_control(client_id, command)
            elif command.command == "release_control":
                result = self._release_control(client_id, command)
            elif command.command == "start_turn":
                result = self._start_turn(client_id, command)
            elif command.command == "cancel_turn":
                result = self._cancel_turn(client_id, command)
            elif command.command == "approve_tool":
                result = self._decide_approval(client_id, command, approved=True)
            elif command.command == "reject_tool":
                result = self._decide_approval(client_id, command, approved=False)
            elif command.command == "select_session":
                result = self._select_session(client_id, command)
            elif command.command == "new_session":
                result = self._new_session(client_id, command)
            else:  # pragma: no cover - Pydantic rejects unknown commands.
                raise CommandError("unknown_command", "Unknown command")
        except CommandError as error:
            result = self._result(command, "rejected", error.message, error.code)
        with self._lock:
            if len(self._command_results) >= 128:
                self._command_results.pop(next(iter(self._command_results)))
            self._command_results[command.command_id] = result
        return result

    def close(self) -> None:
        with self._lock:
            self._closed = True
            cancel = self._cancel
            self._control_client_id = None
            self._reject_pending_locked("Workbench is shutting down")
        if cancel is not None:
            cancel.set()
        self._publish("service_closing", {})

    def _acquire_control(
        self, client_id: str, command: ClientCommand
    ) -> dict[str, Any]:
        with self._lock:
            self._require_revision(command.expected_revision)
            if self._control_client_id not in {None, client_id}:
                raise CommandError(
                    "control_unavailable", "Another local tab has control"
                )
            self._control_client_id = client_id
        self._publish("control_changed", {"holder": client_id})
        return self._result(command, "accepted", "Control acquired")

    def _release_control(
        self, client_id: str, command: ClientCommand
    ) -> dict[str, Any]:
        with self._lock:
            self._require_control(client_id)
            self._require_revision(command.expected_revision)
            self._control_client_id = None
            self._reject_pending_locked("Control lease released")
        self._publish("control_changed", {"holder": None})
        return self._result(command, "accepted", "Control released")

    def _start_turn(self, client_id: str, command: ClientCommand) -> dict[str, Any]:
        prompt = command.payload.get("prompt")
        if not isinstance(prompt, str):
            raise CommandError("invalid_prompt", "Prompt must be text")
        prompt = prompt.strip()
        if not prompt or len(prompt) > MAX_PROMPT_CHARS:
            raise CommandError(
                "invalid_prompt", f"Prompt must contain 1-{MAX_PROMPT_CHARS} characters"
            )
        with self._lock:
            self._require_control(client_id)
            self._require_revision(command.expected_revision)
            if self._run.status in {"running", "cancelling"}:
                raise CommandError("run_conflict", "A turn is already active")
            now = self._now()
            run_id = f"run-{secrets.token_hex(16)}"
            cancel = Event()
            self._cancel = cancel
            self._steps.clear()
            self._output.clear()
            self._quality_checks.clear()
            self._approval = None
            self._run = RunDto(
                status="running",
                run_id=run_id,
                objective=prompt[:500],
                started_at=now,
            )
        self._publish("run_state", self._run.model_dump(mode="json"))
        Thread(
            target=self._execute_turn,
            args=(run_id, prompt, cancel),
            name=f"neil-web-{run_id}",
            daemon=True,
        ).start()
        return self._result(command, "accepted", "Turn started", run_id=run_id)

    def _cancel_turn(self, client_id: str, command: ClientCommand) -> dict[str, Any]:
        with self._lock:
            self._require_control(client_id)
            self._require_revision(command.expected_revision)
            if (
                self._run.status not in {"running", "cancelling"}
                or self._cancel is None
            ):
                raise CommandError("no_active_run", "No active turn can be cancelled")
            self._cancel.set()
            self._reject_pending_locked("Turn cancellation requested")
            self._run = self._run.model_copy(update={"status": "cancelling"})
        self._publish("run_state", self._run.model_dump(mode="json"))
        return self._result(command, "accepted", "Cancellation requested")

    def _execute_turn(self, run_id: str, prompt: str, cancel: Event) -> None:
        with self._lock:
            context = TurnContext(
                messages=self._session_messages,
                last_usage=self._session_usage,
                steps=self._session_steps,
                latest_quality_check=self._session_quality,
            )
        result: TurnResult | None = None
        try:
            result = self._worker.run(
                prompt,
                cancel,
                lambda text: self._record_text(run_id, text),
                lambda event: self._record_activity(run_id, event),
                lambda event: self._record_runtime(run_id, event),
                lambda call, preview: self._request_approval(
                    run_id, call, preview, cancel
                ),
                context,
            )
        except TurnCancelled:
            self._finish_run(run_id, "cancelled")
        except BaseException as error:  # noqa: BLE001 - worker isolation boundary.
            if cancel.is_set():
                self._finish_run(run_id, "cancelled")
            else:
                self._finish_run(run_id, "failed", error_type=type(error).__name__)
        else:
            if cancel.is_set():
                self._finish_run(run_id, "cancelled")
                return
            if result is not None:
                self._persist_turn(result)
            self._finish_run(run_id, "completed")

    def _record_text(self, run_id: str, text: str) -> None:
        if not text:
            return
        safe = text[:4_000]
        with self._lock:
            if self._run.run_id != run_id:
                return
            self._output.append(
                OutputEntryDto(kind="assistant", text=safe, timestamp=self._now())
            )
        self._publish("assistant_text_delta", {"run_id": run_id, "text": safe})

    def _record_activity(self, run_id: str, event: ActivityEvent) -> None:
        text = event.message[:2_000]
        with self._lock:
            if self._run.run_id != run_id:
                return
            self._output.append(
                OutputEntryDto(kind="activity", text=text, timestamp=self._now())
            )
        self._publish(
            "activity",
            {"run_id": run_id, "status": event.status, "message": text},
        )

    def _record_runtime(self, run_id: str, event: RuntimeEvent) -> None:
        metadata = event.metadata_dict()
        status_map = {
            "started": "running",
            "waiting": "waiting_for_approval",
            "succeeded": "succeeded",
            "failed": "failed",
            "skipped": "skipped",
        }
        step_status = cast(
            Literal[
                "pending",
                "running",
                "waiting_for_approval",
                "succeeded",
                "failed",
                "skipped",
                "cancelled",
            ],
            status_map[event.status],
        )
        title = {
            "agent_turn": "Agent turn",
            "model_request": "Model request",
            "tool_call": f"Tool · {metadata.get('tool_name', 'operation')}",
            "approval": "Single-tool approval",
            "quality_check": "Quality check",
        }[event.stage]
        step = RuntimeStepDto(
            correlation_id=event.correlation_id,
            stage=event.stage,
            title=title,
            status=step_status,
            timestamp=event.timestamp,
            metadata=metadata,
        )
        with self._lock:
            if self._run.run_id != run_id:
                return
            if (
                event.correlation_id not in self._steps
                and len(self._steps) >= MAX_RUNTIME_STEPS
            ):
                self._steps.pop(next(iter(self._steps)))
            self._steps[event.correlation_id] = step
            if event.stage == "quality_check" and event.status in {
                "succeeded",
                "failed",
                "skipped",
            }:
                quality_status = cast(
                    Literal["passed", "failed", "not_run"],
                    {
                        "succeeded": "passed",
                        "failed": "failed",
                        "skipped": "not_run",
                    }[event.status],
                )
                self._quality_checks.append(
                    QualityCheckDto(
                        check="run_quality_check",
                        status=quality_status,
                    )
                )
        self._publish(
            "runtime_step", {"run_id": run_id, "step": step.model_dump(mode="json")}
        )

    def _finish_run(
        self,
        run_id: str,
        status: Literal["completed", "failed", "cancelled"],
        *,
        error_type: str | None = None,
    ) -> None:
        with self._lock:
            if self._run.run_id != run_id:
                return
            self._run = self._run.model_copy(
                update={
                    "status": status,
                    "finished_at": self._now(),
                    "error_type": None if error_type is None else error_type[:120],
                }
            )
            self._cancel = None
            self._reject_pending_locked("Run ended before approval resolved")
        self._publish("run_state", self._run.model_dump(mode="json"))

    def _resume_latest_session(self) -> None:
        try:
            index = self._session_store.list_sessions(page=1, page_size=1)
        except SessionError:
            return
        if not index.sessions:
            return
        try:
            snapshot = self._session_store.load(index.sessions[0].session_id)
        except SessionError:
            return
        self._apply_loaded_snapshot(snapshot)

    def _apply_loaded_snapshot(self, snapshot: SessionSnapshot) -> None:
        self._session_handle = self._session_store.handle_for(snapshot)
        self._session_messages = snapshot.messages
        self._session_usage = snapshot.last_usage
        self._session_steps = snapshot.restored_steps()
        self._session_quality = snapshot.restored_quality_check()

    def _reset_unsaved_session(self) -> None:
        self._session_handle = self._session_store.new_session()
        self._session_messages = ()
        self._session_usage = None
        self._session_steps = ()
        self._session_quality = None

    def _persist_turn(self, result: TurnResult) -> None:
        with self._lock:
            self._session_messages = result.messages
            self._session_usage = result.last_usage
            self._session_steps = result.steps
            self._session_quality = result.latest_quality_check
            handle = self._session_handle
        try:
            snapshot = self._session_store.save(
                handle,
                result.messages,
                result.steps,
                result.latest_quality_check,
                last_usage=result.last_usage,
            )
        except SessionError:
            self._publish_session_state()
            return
        with self._lock:
            self._apply_loaded_snapshot(snapshot)
        self._publish_session_state()

    def _select_session(self, client_id: str, command: ClientCommand) -> dict[str, Any]:
        session_id = command.payload.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise CommandError("invalid_session", "Session ID is required")
        session_id = session_id.strip()
        with self._lock:
            self._require_control(client_id)
            self._require_revision(command.expected_revision)
            self._require_idle_session_command()
            current_id = self._session_handle.session_id
            unsaved = not self._session_store.has_saved(current_id)
        if session_id == current_id and unsaved:
            return self._result(
                command, "accepted", "Current session is already selected"
            )
        try:
            snapshot = self._session_store.load(session_id)
        except SessionError as error:
            raise CommandError("session_unavailable", str(error)) from error
        with self._lock:
            self._apply_loaded_snapshot(snapshot)
        self._publish_session_state()
        return self._result(command, "accepted", "Session restored")

    def _new_session(self, client_id: str, command: ClientCommand) -> dict[str, Any]:
        with self._lock:
            self._require_control(client_id)
            self._require_revision(command.expected_revision)
            self._require_idle_session_command()
            self._reset_unsaved_session()
        self._publish_session_state()
        return self._result(command, "accepted", "New session started")

    def _require_idle_session_command(self) -> None:
        if self._run.status in {"running", "cancelling"}:
            raise CommandError(
                "run_conflict", "Session cannot change while a turn is active"
            )
        if self._approval is not None and self._approval.request.state == "pending":
            raise CommandError(
                "approval_pending", "Session cannot change while a tool is waiting"
            )

    def _publish_session_state(self) -> None:
        with self._lock:
            payload = {
                "session": self._active_session_dto().model_dump(mode="json"),
                "task": self._task_dto().model_dump(mode="json"),
                "context": self._context_dto().model_dump(mode="json"),
            }
        self._publish("session_changed", payload)

    def _sessions_overlay(self, base: SessionListDto) -> SessionListDto:
        active = self._active_session_dto()
        saved_ids = {item.session_id for item in base.items}
        items = [item for item in base.items if item.session_id != active.session_id]
        items.insert(0, active)
        extra = 0 if active.session_id in saved_ids else 1
        return SessionListDto(
            available=True,
            items=tuple(items[:20]),
            invalid_count=base.invalid_count,
            total_count=base.total_count + extra,
            active_session_id=active.session_id,
        )

    def _active_session_dto(self) -> SessionDto:
        try:
            if self._session_store.has_saved(self._session_handle.session_id):
                summary = self._session_store.get_summary(
                    self._session_handle.session_id
                )
                return SessionDto(
                    session_id=summary.session_id,
                    title=summary.title,
                    updated_at=summary.updated_at,
                    round_count=summary.round_count,
                    preview=summary.preview,
                    has_plan=summary.has_plan,
                    failed_check=summary.failed_check,
                    has_compaction=summary.has_compaction,
                )
        except SessionError:
            pass
        quality = self._session_quality
        return SessionDto(
            session_id=self._session_handle.session_id,
            title=self._session_handle.title or "New session",
            updated_at=self._session_handle.created_at,
            round_count=sum(
                1 for message in self._session_messages if message.role == "user"
            ),
            preview="",
            has_plan=bool(self._session_steps),
            failed_check=quality is not None and quality.status == "failed",
            has_compaction=False,
        )

    def _task_dto(self) -> TaskDto:
        has_state = bool(self._session_messages or self._session_steps)
        saved = False
        try:
            saved = self._session_store.has_saved(self._session_handle.session_id)
        except SessionError:
            saved = False
        return TaskDto(
            source="saved_session" if has_state or saved else "unavailable",
            session_id=self._session_handle.session_id,
            steps=tuple(
                TaskStepDto(title=step.title, status=step.status)
                for step in self._session_steps
            ),
        )

    def _context_dto(self) -> ContextDto:
        usage = self._session_usage
        limit = self._service.settings.max_context_tokens
        if usage is None:
            return ContextDto(source="unavailable", limit_tokens=limit)
        return ContextDto(
            source="server_reported",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            limit_tokens=limit,
        )

    def _restored_quality_checks(
        self, fallback: tuple[QualityCheckDto, ...]
    ) -> tuple[QualityCheckDto, ...]:
        record = self._session_quality
        if record is None:
            return fallback
        return (
            QualityCheckDto(
                check=record.check,
                status=record.status,
                exit_code=record.exit_code,
            ),
        )

    def _request_approval(
        self,
        run_id: str,
        call: ToolCall,
        preview: str,
        cancel: Event,
    ) -> bool:
        now = self._now()
        request = ApprovalRequestDto(
            request_id=f"approval-{secrets.token_hex(16)}",
            run_id=run_id,
            tool_name=call.name[:128],
            preview=preview[:30_000],
            created_at=now,
            expires_at=now + APPROVAL_TTL,
            state="pending",
        )
        with self._approval_condition:
            if (
                self._closed
                or cancel.is_set()
                or self._run.run_id != run_id
                or (
                    self._approval is not None
                    and self._approval.request.state == "pending"
                )
            ):
                return False
            pending = PendingApproval(request=request)
            self._approval = pending
        self._publish(
            "approval_requested", {"approval": request.model_dump(mode="json")}
        )
        with self._approval_condition:
            while pending.decision is None:
                if self._closed or cancel.is_set():
                    pending.decision = False
                    break
                remaining = (request.expires_at - self._now()).total_seconds()
                if remaining <= 0:
                    pending.request = request.model_copy(
                        update={
                            "state": "expired",
                            "decision_detail": "Approval expired before a decision",
                        }
                    )
                    pending.decision = False
                    break
                self._approval_condition.wait(timeout=min(remaining, 0.25))
            approved = pending.decision is True
            final_request = pending.request
        self._publish(
            "approval_resolved",
            {"approval": final_request.model_dump(mode="json")},
        )
        return approved

    def _decide_approval(
        self,
        client_id: str,
        command: ClientCommand,
        *,
        approved: bool,
    ) -> dict[str, Any]:
        request_id = command.payload.get("request_id")
        if not isinstance(request_id, str):
            raise CommandError("invalid_approval", "Approval request ID is required")
        with self._approval_condition:
            self._require_control(client_id)
            self._require_revision(command.expected_revision)
            pending = self._approval
            if pending is None or pending.request.request_id != request_id:
                raise CommandError(
                    "approval_stale", "Approval request is no longer current"
                )
            if pending.request.state != "pending" or pending.decision is not None:
                raise CommandError("approval_resolved", "Approval was already resolved")
            if self._now() >= pending.request.expires_at:
                pending.request = pending.request.model_copy(
                    update={
                        "state": "expired",
                        "decision_detail": "Approval expired before a decision",
                    }
                )
                pending.decision = False
                self._approval_condition.notify_all()
                raise CommandError("approval_expired", "Approval request has expired")
            state = "approved" if approved else "rejected"
            pending.request = pending.request.model_copy(
                update={
                    "state": state,
                    "decision_detail": (
                        "One tool approved for preview revalidation"
                        if approved
                        else "One tool rejected"
                    ),
                }
            )
            pending.decision = approved
            self._approval_condition.notify_all()
        return self._result(
            command,
            "accepted",
            "One tool approved" if approved else "One tool rejected",
        )

    def _reject_pending_locked(self, detail: str) -> None:
        pending = self._approval
        if pending is None or pending.decision is not None:
            return
        pending.request = pending.request.model_copy(
            update={"state": "stale", "decision_detail": detail[:240]}
        )
        pending.decision = False
        self._approval_condition.notify_all()

    @staticmethod
    def _review_state(
        base_state: ReviewState, approval: ApprovalRequestDto | None
    ) -> ReviewState:
        if approval is None:
            return base_state
        if approval.state == "pending":
            return "approval_required"
        if approval.state == "approved":
            return "applied"
        if approval.state in {"expired", "stale"}:
            return "stale"
        if approval.state == "rejected":
            return base_state
        return base_state

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._sequence += 1
            self._revision += 1
            event = {
                "protocol_version": PROTOCOL_VERSION,
                "message_type": "event",
                "event_type": event_type,
                "sequence": self._sequence,
                "revision": self._revision,
                "timestamp": self._now().isoformat(),
                "payload": payload,
            }
            self._events.append(event)
            subscribers = tuple(self._subscribers.values())
        for loop, subscription in subscribers:
            loop.call_soon_threadsafe(self._enqueue, subscription, event)

    def _enqueue(
        self, subscription: ControllerSubscription, event: dict[str, Any]
    ) -> None:
        if subscription.invalidated:
            return
        try:
            subscription.queue.put_nowait(event)
        except asyncio.QueueFull:
            while not subscription.queue.empty():
                try:
                    subscription.queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - one event loop.
                    break
            subscription.queue.put_nowait(self._invalidation("slow_client"))
            subscription.invalidated = True

    def _invalidation(self, reason: str) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": "event",
            "event_type": "snapshot_invalidated",
            "sequence": self._sequence,
            "revision": self._revision,
            "timestamp": self._now().isoformat(),
            "payload": {"reason": reason},
        }

    def _result(
        self,
        command: ClientCommand,
        status: Literal["accepted", "rejected"],
        detail: str,
        code: str | None = None,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            return {
                "protocol_version": PROTOCOL_VERSION,
                "message_type": "command_result",
                "command_id": command.command_id,
                "status": status,
                "detail": detail,
                "code": code,
                "run_id": run_id,
                "sequence": self._sequence,
                "revision": self._revision,
            }

    def _require_revision(self, revision: int) -> None:
        if revision != self._revision:
            raise CommandError("revision_conflict", "Snapshot revision is stale")

    def _require_control(self, client_id: str) -> None:
        if self._control_client_id != client_id:
            raise CommandError("control_required", "This tab does not hold control")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("controller clock must return an aware timestamp")
        return now
