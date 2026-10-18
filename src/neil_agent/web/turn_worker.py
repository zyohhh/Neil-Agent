"""Execute one Agent turn; the controller owns leases, approvals and persistence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Protocol

from ..config import Settings
from ..execution_budget import execution_budget_scope
from ..events import EventBus, RuntimeEvent
from ..host_runtime import HostMode, HostRuntime, build_agent, build_host_runtime
from ..providers.factory import create_provider
from ..schemas import ActivityEvent, Message, TokenUsage, ToolCall
from ..session import SessionSnapshot
from ..subtask import SubtaskParentState, subtask_parent_scope
from ..task import QualityCheckRecord, TaskStep


class TurnCancelled(Exception):
    """Internal cooperative cancellation signal with no user content."""


TextSink = Callable[[str], None]
ActivitySink = Callable[[ActivityEvent], None]
RuntimeSink = Callable[[RuntimeEvent], None]
ApprovalSink = Callable[[ToolCall, str], bool]


@dataclass(frozen=True, slots=True)
class CompletedTurnState:
    """Complete server-side state eligible for one atomic session save."""

    messages: tuple[Message, ...]
    steps: tuple[TaskStep, ...]
    latest_quality_check: QualityCheckRecord | None
    last_usage: TokenUsage | None
    execution_id: str | None = None


class TurnWorker(Protocol):
    """Synchronous worker boundary executed on one daemon thread."""

    def run(
        self,
        prompt: str,
        session: SessionSnapshot | None,
        cancel: Event,
        on_text: TextSink,
        on_activity: ActivitySink,
        on_runtime: RuntimeSink,
        request_approval: ApprovalSink,
        *,
        parent_run_id: str | None = None,
    ) -> CompletedTurnState | None: ...


TurnWorkerFactory = Callable[[Settings], TurnWorker]


class AgentTurnWorker:
    """Run the configured Agent with preview-gated mutation tools for P3."""

    def __init__(
        self,
        settings: Settings,
        *,
        host_runtime: HostRuntime | None = None,
        host_runtime_provider: Callable[[], HostRuntime] | None = None,
    ) -> None:
        if host_runtime is not None and host_runtime_provider is not None:
            raise ValueError("pass host_runtime or host_runtime_provider, not both")
        self._settings = settings
        self._host_runtime = host_runtime
        self._host_runtime_provider = host_runtime_provider

    def run(
        self,
        prompt: str,
        session: SessionSnapshot | None,
        cancel: Event,
        on_text: TextSink,
        on_activity: ActivitySink,
        on_runtime: RuntimeSink,
        request_approval: ApprovalSink,
        *,
        parent_run_id: str | None = None,
    ) -> CompletedTurnState:
        settings = self._settings
        owned_runtime = False
        if self._host_runtime_provider is not None:
            host_runtime = self._host_runtime_provider()
        elif self._host_runtime is not None:
            host_runtime = self._host_runtime
        else:
            host_runtime = build_host_runtime(settings, mode=HostMode.WEB)
            owned_runtime = True
        if host_runtime.closed:
            raise RuntimeError("Web host runtime is closed")
        try:
            task_tracker = host_runtime.task_tracker
            if task_tracker is None:
                raise RuntimeError("Web host runtime must include a task tracker.")
            if session is None:
                task_tracker.clear()
            bus = EventBus(queue_size=256, max_observers=1)
            subscription = bus.subscribe(on_runtime)
            model = create_provider(settings, retry_handler=on_activity)
            agent = build_agent(
                settings,
                host_runtime,
                model,
                approval_handler=request_approval,
                activity_handler=on_activity,
                event_bus=bus,
            )
            agent.bind_session(None if session is None else session.session_id)
            if session is not None:
                agent.restore_messages(session.messages, session.last_usage)
                task_tracker.restore(
                    session.restored_steps(), session.restored_quality_check()
                )
            with (
                execution_budget_scope(
                    cancel=cancel, cancelled_message="当前任务已取消。"
                ),
                subtask_parent_scope(
                    SubtaskParentState(
                        settings=settings,
                        model=model,
                        parent_run_id=parent_run_id,
                        forward_runtime_event=on_runtime,
                        cancel=cancel,
                    )
                ),
            ):
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
                    return CompletedTurnState(
                        messages=agent.messages,
                        steps=task_tracker.steps,
                        latest_quality_check=task_tracker.latest_quality_check,
                        last_usage=agent.last_usage,
                        execution_id=agent.last_execution_id,
                    )
                finally:
                    subscription.close()
                    bus.close(timeout=0.5)
        finally:
            if owned_runtime:
                host_runtime.close()
