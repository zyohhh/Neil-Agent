"""DeepSeek adapter for its Anthropic-compatible Messages endpoint."""

from __future__ import annotations

from anthropic.types import ThinkingConfigParam

from .anthropic_runtime import AnthropicMessagesProvider
from .base import (
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderId,
    WireProtocol,
)
from .errors import ProviderProtocolError

DEEPSEEK_DESCRIPTOR = ProviderDescriptor(
    provider=ProviderId.DEEPSEEK,
    display_name="DeepSeek",
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


class DeepSeekProvider(AnthropicMessagesProvider):
    """DeepSeek profile over the shared Anthropic Messages runtime."""

    provider_id = ProviderId.DEEPSEEK
    provider_descriptor = DEEPSEEK_DESCRIPTOR

    def _base_url(self) -> str:
        base_url = super()._base_url()
        if base_url is None:
            raise ProviderProtocolError(
                "未配置 DeepSeek API 地址。",
                provider=self.provider_id,
            )
        return base_url

    def _thinking_config(self) -> ThinkingConfigParam:
        if self.settings.thinking_enabled:
            # DeepSeek accepts this field but ignores budget_tokens.
            return {"type": "enabled", "budget_tokens": 1024}
        return {"type": "disabled"}
