"""Conservative profiles for local OpenAI-compatible Responses servers."""

from __future__ import annotations

from ..config import Settings
from .base import (
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderId,
    WireProtocol,
)
from .openai import OpenAIResponsesProvider

OLLAMA_DESCRIPTOR = ProviderDescriptor(
    provider=ProviderId.OLLAMA,
    display_name="Ollama",
    wire_protocol=WireProtocol.OPENAI_COMPATIBLE,
    capabilities=ProviderCapabilities(
        streaming=True,
        tool_calling=False,
        parallel_tool_calls=False,
        reasoning_state=False,
        structured_output=False,
        usage_reporting=True,
        prompt_caching=False,
    ),
)

VLLM_DESCRIPTOR = ProviderDescriptor(
    provider=ProviderId.VLLM,
    display_name="vLLM",
    wire_protocol=WireProtocol.OPENAI_COMPATIBLE,
    capabilities=ProviderCapabilities(
        streaming=True,
        tool_calling=False,
        parallel_tool_calls=False,
        reasoning_state=False,
        structured_output=False,
        usage_reporting=True,
        prompt_caching=False,
    ),
)


class OpenAICompatibleProvider(OpenAIResponsesProvider):
    """Shared local Responses runtime with conservative request semantics."""

    requires_api_key = False
    send_store_field = False
    send_empty_tools = False

    def _configured_descriptor(self) -> ProviderDescriptor:
        """Freeze capabilities for this exact local deployment configuration."""

        return configured_local_descriptor(self.settings, self.provider_descriptor)


class OllamaProvider(OpenAICompatibleProvider):
    """Ollama Responses profile; SDK requires an ignored placeholder key."""

    provider_id = ProviderId.OLLAMA
    provider_descriptor = OLLAMA_DESCRIPTOR
    placeholder_api_key = "ollama"


class VLLMProvider(OpenAICompatibleProvider):
    """vLLM Responses profile with an explicit local placeholder key."""

    provider_id = ProviderId.VLLM
    provider_descriptor = VLLM_DESCRIPTOR
    placeholder_api_key = "EMPTY"
    send_parallel_tool_calls = True


def configured_local_descriptor(
    settings: Settings,
    profile: ProviderDescriptor,
) -> ProviderDescriptor:
    """Return the conservative capability snapshot used by runtime and doctor."""

    if profile.provider not in {ProviderId.OLLAMA, ProviderId.VLLM}:
        raise ValueError("local capability configuration requires a local provider")
    if settings.llm_provider is not profile.provider:
        raise ValueError("local capability profile does not match selected provider")
    base = profile.capabilities
    return ProviderDescriptor(
        provider=profile.provider,
        display_name=profile.display_name,
        wire_protocol=profile.wire_protocol,
        capabilities=ProviderCapabilities(
            streaming=base.streaming,
            tool_calling=settings.local_tool_calling_enabled,
            parallel_tool_calls=(
                profile.provider is ProviderId.VLLM
                and settings.local_parallel_tool_calls_enabled
            ),
            reasoning_state=base.reasoning_state,
            structured_output=base.structured_output,
            usage_reporting=base.usage_reporting,
            prompt_caching=base.prompt_caching,
        ),
    )
