"""Native Claude adapter for the Anthropic Messages API."""

from __future__ import annotations

from anthropic.types import ThinkingConfigParam

from .anthropic_runtime import AnthropicMessagesProvider
from .base import (
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderId,
    WireProtocol,
)

CLAUDE_DESCRIPTOR = ProviderDescriptor(
    provider=ProviderId.CLAUDE,
    display_name="Claude",
    wire_protocol=WireProtocol.ANTHROPIC_MESSAGES,
    capabilities=ProviderCapabilities(
        streaming=True,
        tool_calling=True,
        parallel_tool_calls=True,
        reasoning_state=True,
        structured_output=False,
        usage_reporting=True,
        prompt_caching=False,
    ),
)


class ClaudeProvider(AnthropicMessagesProvider):
    """Claude profile over the shared Anthropic Messages runtime."""

    provider_id = ProviderId.CLAUDE
    provider_descriptor = CLAUDE_DESCRIPTOR

    def _thinking_config(self) -> ThinkingConfigParam | None:
        if not self.settings.thinking_enabled:
            # Native Anthropic requests omit thinking when it is not enabled.
            return None
        if self.settings.claude_thinking_mode == "adaptive":
            return {"type": "adaptive"}
        return {
            "type": "enabled",
            "budget_tokens": self.settings.claude_thinking_budget_tokens,
        }
