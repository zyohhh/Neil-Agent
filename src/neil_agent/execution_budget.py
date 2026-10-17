"""Cooperative cancellation, deadlines, and interruption of active model streams."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Thread
from time import monotonic

from .errors import ToolError

_CANCELLED_MESSAGE = "只读子任务已取消。"
_TIMEOUT_MESSAGE = "只读子任务超时。"


@dataclass(slots=True)
class ExecutionBudget:
    """Optional wall-clock and cancel signals checked during tool I/O."""

    deadline: float | None = None
    cancel: Event | None = None
    cancelled_message: str = _CANCELLED_MESSAGE
    parent: ExecutionBudget | None = None


_budget: ContextVar[ExecutionBudget | None] = ContextVar(
    "execution_budget", default=None
)


@contextmanager
def execution_budget_scope(
    *,
    deadline: float | None = None,
    cancel: Event | None = None,
    cancelled_message: str = _CANCELLED_MESSAGE,
):
    """Bind a turn or subtask budget without discarding its parent's limits."""

    token = _budget.set(
        ExecutionBudget(
            deadline=deadline,
            cancel=cancel,
            cancelled_message=cancelled_message,
            parent=_budget.get(),
        )
    )
    try:
        yield
    finally:
        _budget.reset(token)


def check_execution_budget() -> None:
    """Fail closed when the active budget is cancelled or past its deadline."""

    _check_budget(_budget.get())


def _check_budget(budget: ExecutionBudget | None) -> None:
    while budget is not None:
        if budget.cancel is not None and budget.cancel.is_set():
            raise ToolError(budget.cancelled_message)
        if budget.deadline is not None and monotonic() >= budget.deadline:
            raise ToolError(_TIMEOUT_MESSAGE)
        budget = budget.parent


def budgeted_sleep(delay: float, sleeper: Callable[[float], None]) -> None:
    """Make retry backoff responsive while retaining injectable test sleepers."""

    check_execution_budget()
    if _budget.get() is None:
        sleeper(delay)
    else:
        remaining = delay
        while remaining > 0:
            check_execution_budget()
            step = min(remaining, 0.05)
            sleeper(step)
            remaining -= step
    check_execution_budget()


@contextmanager
def interrupt_on_execution_budget(interrupt: Callable[[], object] | None):
    """Close an established SDK stream if its active budget is interrupted."""

    budget = _budget.get()
    check_execution_budget()
    if budget is None or not callable(interrupt):
        yield
        return
    settled = Event()

    def watch() -> None:
        while not settled.wait(0.02):
            try:
                _check_budget(budget)
            except ToolError:
                try:
                    interrupt()
                except Exception:
                    # The normal execution path still observes the budget.
                    pass
                return

    watcher = Thread(target=watch, name="neil-agent-model-cancel", daemon=True)
    watcher.start()
    try:
        yield
        check_execution_budget()
    finally:
        settled.set()
        watcher.join(timeout=0.1)
