"""Typed, in-process lifecycle hooks with bounded control responses."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from unicodedata import category

from .errors import HookError
from .schemas import ModelResponse, ToolCall, ToolResult

HookStage = Literal["before_model", "after_model", "before_tool", "after_tool"]
HookDecision = Literal["allow", "deny"]
MAX_HOOKS_PER_STAGE = 10
MAX_HOOK_REASON_CHARS = 500
MAX_HOOK_CONTEXT_CHARS = 2_000
MAX_HOOK_CONTEXT_TOTAL_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class HookEvent:
    """One typed lifecycle observation exposed only to trusted Python callbacks."""

    stage: HookStage
    model_round: int = 0
    message_count: int = 0
    model_response: ModelResponse | None = field(default=None, repr=False)
    tool_call: ToolCall | None = field(default=None, repr=False)
    tool_result: ToolResult | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class HookResponse:
    """A bounded pre-event decision; audit-only hooks normally return ``None``."""

    decision: HookDecision = "allow"
    reason: str = ""
    additional_context: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class HookOutcome:
    """Combined, validated result of every callback for one lifecycle event."""

    allowed: bool = True
    reason: str = ""
    additional_context: str = field(default="", repr=False)


HookCallback = Callable[[HookEvent], HookResponse | None]


class LifecycleHooks:
    """Register trusted callbacks without parsing or executing shell strings."""

    def __init__(self) -> None:
        self._callbacks: dict[HookStage, list[HookCallback]] = {
            "before_model": [],
            "after_model": [],
            "before_tool": [],
            "after_tool": [],
        }

    def register(self, stage: HookStage, callback: HookCallback) -> None:
        """Register one callable for an exact stage with a small fan-out limit."""

        callbacks = self._callbacks.get(stage)
        if callbacks is None:
            raise HookError(f"未知的生命周期 hook 阶段：{stage}")
        if len(callbacks) >= MAX_HOOKS_PER_STAGE:
            raise HookError(
                f"每个生命周期阶段最多注册 {MAX_HOOKS_PER_STAGE} 个 hooks。"
            )
        if not callable(callback):
            raise HookError("生命周期 hook 必须是可调用的 Python 对象。")
        callbacks.append(callback)

    def dispatch(self, event: HookEvent) -> HookOutcome:
        """Run callbacks in order and fail closed on invalid control output."""

        allow_control = event.stage in {"before_model", "before_tool"}
        allow_context = event.stage == "before_model"
        contexts: list[str] = []
        total_context_chars = 0
        callbacks = self._callbacks.get(event.stage)
        if callbacks is None:
            raise HookError(f"未知的生命周期 hook 阶段：{event.stage}")
        for callback in tuple(callbacks):
            try:
                response = callback(event)
            except Exception as error:  # noqa: BLE001 - trusted hook boundary.
                raise HookError(
                    f"{event.stage} hook 执行失败（{type(error).__name__}）。"
                ) from error
            if response is None:
                continue
            if not isinstance(response, HookResponse):
                raise HookError(f"{event.stage} hook 返回了无效响应类型。")
            if response.decision not in {"allow", "deny"}:
                raise HookError(f"{event.stage} hook 返回了无效决策。")
            reason = _bounded_hook_text(
                response.reason,
                label="hook reason",
                max_chars=MAX_HOOK_REASON_CHARS,
                multiline=False,
            )
            context = _bounded_hook_text(
                response.additional_context,
                label="hook additional_context",
                max_chars=MAX_HOOK_CONTEXT_CHARS,
                multiline=True,
            )
            if not allow_control and response.decision != "allow":
                raise HookError(f"{event.stage} 是只读审计阶段，不能拒绝操作。")
            if not allow_context and context:
                raise HookError(f"{event.stage} 不允许附加模型上下文。")
            if response.decision == "deny":
                return HookOutcome(
                    allowed=False,
                    reason=reason or "本地生命周期 hook 拒绝了该操作。",
                )
            if context:
                total_context_chars += len(context)
                if total_context_chars > MAX_HOOK_CONTEXT_TOTAL_CHARS:
                    raise HookError("生命周期 hooks 的附加上下文累计超过上限。")
                contexts.append(context)
        return HookOutcome(additional_context="\n\n".join(contexts))


def _bounded_hook_text(
    value: str,
    *,
    label: str,
    max_chars: int,
    multiline: bool,
) -> str:
    if not isinstance(value, str):
        raise HookError(f"{label} 必须是字符串。")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) > max_chars:
        raise HookError(f"{label} 超过 {max_chars} 字符上限。")
    if not multiline and "\n" in normalized:
        raise HookError(f"{label} 必须是单行文本。")
    if any(
        category(character).startswith("C")
        and character not in ({"\n", "\t"} if multiline else set())
        for character in normalized
    ):
        raise HookError(f"{label} 包含不允许的控制或格式字符。")
    return normalized
