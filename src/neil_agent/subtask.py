"""One-shot read-only sub-agent runs with bounded budgets and parent linkage."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING

from .config import Settings
from .errors import NeilAgentError, ToolError
from .events import EventBus, RuntimeEvent, redact_runtime_metadata
from .execution_budget import check_execution_budget, execution_budget_scope
from .host_runtime import HostMode, RuntimeProfile, build_agent, build_host_runtime

if TYPE_CHECKING:
    from .agent import ChatModel

READONLY_SUBTASK_SYSTEM_PROMPT = """You are a read-only exploration sub-agent.
Use only the provided read-only filesystem tools to investigate the repository.
Return a concise factual summary for the main agent.
Do not attempt writes, shell commands, task planning, or nested subtasks."""

RuntimeEventForwarder = Callable[[RuntimeEvent], None]


@dataclass(slots=True)
class SubtaskParentState:
    """Mutable parent-turn context for one synchronous subtask execution."""

    settings: Settings
    model: ChatModel
    parent_run_id: str | None = None
    forward_runtime_event: RuntimeEventForwarder | None = None
    cancel: Event | None = None


_parent_state: ContextVar[SubtaskParentState | None] = ContextVar(
    "subtask_parent_state",
    default=None,
)
_parent_tool_event_id: ContextVar[str | None] = ContextVar(
    "subtask_parent_tool_event_id",
    default=None,
)


@contextmanager
def subtask_parent_scope(state: SubtaskParentState):
    """Bind parent settings and observability for one agent turn."""

    token = _parent_state.set(state)
    try:
        yield state
    finally:
        _parent_state.reset(token)


def note_parent_run_id(parent_run_id: str | None) -> None:
    """Record the active turn id when the host did not pre-bind one."""

    if not parent_run_id:
        return
    state = _parent_state.get()
    if state is not None and state.parent_run_id is None:
        state.parent_run_id = parent_run_id


def new_parent_run_id() -> str:
    """Return a stable parent run id shared by CLI and Web turns."""

    return f"run-{secrets.token_hex(16)}"


@contextmanager
def parent_tool_event_scope(parent_tool_event_id: str | None):
    """Expose the active parent tool span while a tool handler runs."""

    token = _parent_tool_event_id.set(parent_tool_event_id)
    try:
        yield
    finally:
        _parent_tool_event_id.reset(token)


def _require_parent_state() -> SubtaskParentState:
    state = _parent_state.get()
    if state is None:
        raise ToolError("只读子任务仅在 CLI 或 Web 标准运行时可用。")
    return state


def _bound_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = "\n... [subtask summary truncated] ...\n"
    keep = max_chars - len(marker)
    if keep < 2:
        return text[:max_chars]
    front = keep // 2
    back = keep - front
    return text[:front] + marker + text[-back:]


def _forward_subtask_event(
    event: RuntimeEvent,
    *,
    parent_run_id: str,
    parent_event_id: str | None,
) -> RuntimeEvent:
    metadata: dict[str, object] = {item.name: item.value for item in event.metadata}
    metadata["parent_run_id"] = parent_run_id
    return event.model_copy(
        update={
            "metadata": redact_runtime_metadata(event.stage, metadata),
            "parent_event_id": parent_event_id or event.parent_event_id,
        }
    )


def _collect_stream_text(stream: Iterator[str]) -> str:
    chunks: list[str] = []
    for chunk in stream:
        check_execution_budget()
        chunks.append(chunk)
    return "".join(chunks)


def execute_readonly_subtask(prompt: str) -> str:
    """Run one bounded read-only exploration sub-agent and return a summary."""

    parent = _require_parent_state()
    settings = parent.settings
    prompt_text = prompt.strip()
    if not prompt_text:
        raise ToolError("只读子任务提示不能为空。")
    if len(prompt_text) > settings.subtask_max_prompt_chars:
        raise ToolError(
            f"只读子任务提示超过 {settings.subtask_max_prompt_chars} 字符上限。"
        )

    parent_run_id = parent.parent_run_id
    if not parent_run_id:
        raise ToolError("只读子任务缺少父回合标识。")

    child_runtime = build_host_runtime(
        settings,
        mode=HostMode.NONINTERACTIVE_READONLY,
        profile=RuntimeProfile.READONLY_SUBTASK,
    )
    child_bus = EventBus(queue_size=64, max_observers=1)
    subscription = None
    deadline = monotonic() + settings.subtask_timeout_seconds
    try:
        if parent.forward_runtime_event is not None:
            parent_event_id = _parent_tool_event_id.get()

            def forward(event: RuntimeEvent) -> None:
                forwarder = parent.forward_runtime_event
                if forwarder is None:
                    return
                forwarder(
                    _forward_subtask_event(
                        event,
                        parent_run_id=parent_run_id,
                        parent_event_id=parent_event_id,
                    )
                )

            subscription = child_bus.subscribe(forward)

        child_agent = build_agent(
            settings,
            child_runtime,
            parent.model,
            event_bus=child_bus,
            system_prompt=READONLY_SUBTASK_SYSTEM_PROMPT,
            project_instructions="",
            max_tool_rounds=settings.subtask_max_tool_rounds,
            max_context_chars=settings.subtask_max_context_chars,
            max_context_tokens=None,
            attach_task_tracker=False,
            attach_hooks=False,
            attach_checkpoints=False,
            attach_instruction_scope=False,
        )
        with execution_budget_scope(deadline=deadline, cancel=parent.cancel):
            summary = _collect_stream_text(child_agent.stream_chat(prompt_text))
        child_bus.flush(timeout=0.25)
        return _bound_text(summary, settings.subtask_max_result_chars)
    except ToolError:
        raise
    except NeilAgentError as error:
        raise ToolError(str(error)) from error
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException as error:
        raise ToolError(f"只读子任务失败：{type(error).__name__}") from error
    finally:
        if subscription is not None:
            subscription.close()
        child_bus.close(timeout=0.25)
        child_runtime.close()
