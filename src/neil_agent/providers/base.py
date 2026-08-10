"""Stable provider identities, capabilities, and private turn state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
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
        frozen_payload = MappingProxyType(dict(self.payload))
        object.__setattr__(
            self,
            "payload",
            cast(Mapping[str, object], frozen_payload),
        )

    def belongs_to(self, provider: ProviderId, model: str) -> bool:
        """Return whether this state is safe to replay for a target model."""

        return self.provider is provider and self.model == model
