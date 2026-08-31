"""Cooperative cancel and deadline checks for bounded sub-agent execution."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event
from time import monotonic

from .errors import ToolError

_CANCELLED_MESSAGE = "只读子任务已取消。"
_TIMEOUT_MESSAGE = "只读子任务超时。"


@dataclass(slots=True)
class ExecutionBudget:
    """Optional wall-clock and cancel signals checked during tool I/O."""

    deadline: float | None = None
    cancel: Event | None = None


_budget: ContextVar[ExecutionBudget | None] = ContextVar("execution_budget", default=None)


@contextmanager
def execution_budget_scope(*, deadline: float, cancel: Event | None = None):
    """Bind one subtask budget for the current execution context."""

    token = _budget.set(ExecutionBudget(deadline=deadline, cancel=cancel))
    try:
        yield
    finally:
        _budget.reset(token)


def check_execution_budget() -> None:
    """Fail closed when the active budget is cancelled or past its deadline."""

    budget = _budget.get()
    if budget is None:
        return
    if budget.cancel is not None and budget.cancel.is_set():
        raise ToolError(_CANCELLED_MESSAGE)
    if budget.deadline is not None and monotonic() > budget.deadline:
        raise ToolError(_TIMEOUT_MESSAGE)
