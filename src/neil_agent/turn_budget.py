"""One attempt/usage/deadline ledger shared by a turn, its subtasks and retries."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from math import isfinite
from threading import Lock
from time import monotonic
from typing import TypeVar, cast

from .errors import ToolError
from .execution_budget import check_execution_budget, current_execution_deadline, execution_budget_scope
from .schemas import TokenUsage

_Client = TypeVar("_Client")


def budgeted_sdk_client(client: _Client) -> _Client:
    """Keep hidden SDK retries outside the transport without mutating its owner."""

    retries = getattr(client, "max_retries", None)
    if not isinstance(retries, (int, float)) or retries == 0:
        return client
    clone = getattr(client, "with_options", None)
    if not callable(clone):
        raise ValueError("SDK 客户端必须禁用内建重试，交由运行时统一计数。")
    configured = clone(max_retries=0)
    if getattr(configured, "max_retries", None) != 0:
        raise ValueError("无法禁用 SDK 客户端的内建重试。")
    return cast(_Client, configured)


class TurnBudgetExceeded(ToolError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        labels = {
            "model_attempts": "模型请求尝试次数",
            "reported_tokens": "累计已报告 token",
            "deadline": "累计执行时限",
        }
        message = f"当前任务达到{labels[reason]}上限，已停止后续执行。"
        if reason == "deadline":
            message = "当前任务执行超时，达到累计执行时限上限，已停止后续执行。"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TurnLimits:
    max_model_attempts: int = 32
    max_reported_tokens: int | None = 100_000
    timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.max_model_attempts < 1 or (
            self.max_reported_tokens is not None and self.max_reported_tokens < 1
        ) or not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("turn limits must be finite and positive")


@dataclass(frozen=True, slots=True)
class TurnUsage:
    model_attempts: int
    unreported_attempts: int
    usage: TokenUsage | None
    usage_complete: bool
    stop_reason: str | None


class TurnBudget:
    def __init__(self, limits: TurnLimits) -> None:
        self.limits = limits
        self.deadline = monotonic() + limits.timeout_seconds
        self._lock = Lock()
        self._attempts = 0
        self._finished = 0
        self._unreported = 0
        self._usage: TokenUsage | None = None
        self._stop_reason: str | None = None

    def _check_locked(self) -> None:
        if self._stop_reason is None and monotonic() >= self.deadline:
            self._stop_reason = "deadline"
        if self._stop_reason is not None:
            raise TurnBudgetExceeded(self._stop_reason)

    def check(self) -> None:
        with self._lock:
            self._check_locked()

    def _check_next_locked(self) -> None:
        self._check_locked()
        if self._attempts >= self.limits.max_model_attempts:
            self._stop_reason = "model_attempts"
        elif (
            self.limits.max_reported_tokens is not None
            and self._usage is not None
            and self._usage.total_tokens >= self.limits.max_reported_tokens
        ):
            self._stop_reason = "reported_tokens"
        self._check_locked()

    def require_next_attempt(self) -> None:
        with self._lock:
            self._check_next_locked()

    def start_attempt(self) -> None:
        with self._lock:
            self._check_next_locked()
            self._attempts += 1

    def finish_attempt(self, usage: TokenUsage | None) -> None:
        with self._lock:
            self._finished += 1
            if usage is None:
                self._unreported += 1
            else:
                self._usage = usage if self._usage is None else self._usage.combine(usage)

    def snapshot(self) -> TurnUsage:
        with self._lock:
            return TurnUsage(
                model_attempts=self._attempts,
                unreported_attempts=self._unreported + self._attempts - self._finished,
                usage=self._usage,
                usage_complete=self._attempts > 0 and self._finished == self._attempts and not self._unreported,
                stop_reason=self._stop_reason,
            )


_turn: ContextVar[TurnBudget | None] = ContextVar("turn_budget", default=None)


def current_turn_usage() -> TurnUsage | None:
    budget = _turn.get()
    return None if budget is None else budget.snapshot()


def check_next_model_attempt() -> None:
    check_execution_budget()
    budget = _turn.get()
    if budget is not None:
        budget.require_next_attempt()


@contextmanager
def turn_budget_scope(limits: TurnLimits) -> Iterator[TurnBudget]:
    existing = _turn.get()
    if existing is not None:
        check_execution_budget()
        yield existing
        return
    budget = TurnBudget(limits)
    parent_deadline = current_execution_deadline()
    if parent_deadline is not None:
        budget.deadline = min(budget.deadline, parent_deadline)
    token = _turn.set(budget)
    try:
        with execution_budget_scope(deadline=budget.deadline, limit_check=budget.check):
            yield budget
    finally:
        _turn.reset(token)


@dataclass(slots=True)
class _Request:
    budget: TurnBudget | None
    usage: TokenUsage | None = None
    claimed: bool = False


_request: ContextVar[_Request | None] = ContextVar("budget_model_request", default=None)


@contextmanager
def model_request_scope() -> Iterator[_Request]:
    """Reserve the first attempt, including for custom/fixture ChatModels."""

    check_execution_budget()
    budget = _turn.get()
    if budget is not None:
        budget.start_attempt()
    request = _Request(budget)
    token = _request.set(request)
    try:
        yield request
    finally:
        if budget is not None and not request.claimed:
            budget.finish_attempt(request.usage)
        _request.reset(token)


@dataclass(slots=True)
class ModelAttempt:
    usage: TokenUsage | None = None

    def observe_usage(self, raw: object, decoder: Callable[[object], TokenUsage]) -> None:
        counts = [raw.get(name) if isinstance(raw, Mapping) else getattr(raw, name, None) for name in ("input_tokens", "output_tokens")]
        if all(type(value) is int and value >= 0 for value in counts):
            self.usage = decoder(raw)


@contextmanager
def model_attempt_scope() -> Iterator[ModelAttempt]:
    """SDK adapters claim the reserved attempt; every retry starts another."""

    check_execution_budget()
    budget = _turn.get()
    request = _request.get()
    if request is not None and request.budget is budget and not request.claimed:
        request.claimed = True
    elif budget is not None:
        budget.start_attempt()
    attempt = ModelAttempt()
    try:
        yield attempt
    finally:
        if budget is not None:
            budget.finish_attempt(attempt.usage)
