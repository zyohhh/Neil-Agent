"""Stable provider identities, capabilities, and private turn state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import cast


class ProviderId(StrEnum):
    """User-selectable model providers."""

    DEEPSEEK = "deepseek"
    CLAUDE = "claude"
    OPENAI = "openai"
    OLLAMA = "ollama"
    VLLM = "vllm"


class WireProtocol(StrEnum):
    """Actual remote protocol families implemented by adapters."""

    ANTHROPIC_MESSAGES = "anthropic-messages"
    OPENAI_RESPONSES = "openai-responses"
    OPENAI_COMPATIBLE = "openai-compatible"


class StopReason(StrEnum):
    """Provider-neutral terminal reasons frozen by contract version 1."""

    END_TURN = "end_turn"
    TOOL_CALL = "tool_call"
    MAX_TOKENS = "max_tokens"
    CONTENT_FILTER = "content_filter"
    CANCELLED = "cancelled"
    ERROR = "error"
    UNKNOWN = "unknown"


def completion_error(
    reason: StopReason,
    *,
    has_tool_calls: bool,
    allow_unknown: bool = False,
) -> str | None:
    """Reject incomplete terminal states before committing text or running tools."""

    errors = {
        StopReason.MAX_TOKENS: "模型输出达到 token 或上下文上限，响应未完成。",
        StopReason.CONTENT_FILTER: "模型响应被内容过滤中止，任务未完成。",
        StopReason.CANCELLED: "模型响应已取消，任务未完成。",
        StopReason.ERROR: "模型响应以错误终止，任务未完成。",
    }
    if reason in errors:
        return errors[reason]
    if reason is StopReason.UNKNOWN and not allow_unknown:
        return "模型返回了未知终止原因，不能确认任务已完成。"
    if reason is StopReason.TOOL_CALL and not has_tool_calls:
        return "模型以工具调用结束，但没有返回工具调用。"
    if reason is StopReason.END_TURN and has_tool_calls:
        return "模型完成状态与返回的工具调用不一致。"
    return None


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """One immutable snapshot of explicitly supported provider features."""

    streaming: bool
    tool_calling: bool
    parallel_tool_calls: bool
    reasoning_state: bool
    structured_output: bool
    usage_reporting: bool
    prompt_caching: bool


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Safe metadata describing one configured provider implementation."""

    provider: ProviderId
    display_name: str
    wire_protocol: WireProtocol
    capabilities: ProviderCapabilities

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError("provider display name must not be blank")


@dataclass(frozen=True, slots=True)
class ProviderTurnState:
    """Opaque state that can only be replayed by its originating provider."""

    provider: ProviderId
    model: str
    schema_version: int
    payload: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("provider turn state model must not be blank")
        if self.schema_version < 1:
            raise ValueError("provider turn state schema version must be positive")
        frozen_payload = _freeze_provider_mapping(self.payload)
        object.__setattr__(
            self,
            "payload",
            cast(Mapping[str, object], frozen_payload),
        )

    def belongs_to(self, provider: ProviderId, model: str) -> bool:
        """Return whether this state is safe to replay for a target model."""

        return self.provider is provider and self.model == model


def _freeze_provider_mapping(
    value: Mapping[str, object],
) -> Mapping[str, object]:
    frozen: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("provider turn state keys must be strings")
        frozen[key] = _freeze_provider_value(item)
    return MappingProxyType(frozen)


def _freeze_provider_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("provider turn state numbers must be finite")
        return value
    if isinstance(value, Mapping):
        return _freeze_provider_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_provider_value(item) for item in value)
    raise TypeError("provider turn state must contain only JSON-compatible values")
