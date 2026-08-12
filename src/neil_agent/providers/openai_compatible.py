"""Conservative profiles for local OpenAI-compatible Responses servers."""

from __future__ import annotations

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
