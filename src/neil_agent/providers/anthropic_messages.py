"""Anthropic Messages serialization and SDK error normalization."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from anthropic import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)
from anthropic.types import MessageParam, ToolParam

from ..schemas import Message, ToolDefinition
from .base import ProviderId, StopReason
from .errors import (
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderError,
    ProviderInternalError,
    ProviderInvalidRequestError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from .retry import parse_retry_after


def encode_message(message: Message) -> MessageParam:
    """Encode one provider-neutral message as Anthropic content blocks."""

    if not message.thinking and not message.tool_calls and not message.tool_results:
        return cast(MessageParam, {"role": message.role, "content": message.content})

    blocks: list[dict[str, object]] = []
    blocks.extend(
        {
            "type": "thinking",
            "thinking": item.thinking,
            "signature": item.signature,
        }
        for item in message.thinking
    )
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    blocks.extend(
        {
            "type": "tool_use",
            "id": item.id,
            "name": item.name,
            "input": item.arguments,
        }
        for item in message.tool_calls
    )
    blocks.extend(
        {
            "type": "tool_result",
            "tool_use_id": item.tool_call_id,
            "content": item.content,
            "is_error": item.is_error,
        }
        for item in message.tool_results
    )
    return cast(MessageParam, {"role": message.role, "content": blocks})


def encode_messages(messages: Sequence[Message]) -> list[MessageParam]:
    """Encode a complete request history for Anthropic Messages."""

    return [encode_message(message) for message in messages]


def encode_tool(tool: ToolDefinition) -> ToolParam:
    """Encode one provider-neutral tool definition for Anthropic Messages."""

    return cast(
        ToolParam,
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        },
    )


def encode_tools(tools: Sequence[ToolDefinition]) -> list[ToolParam]:
    """Encode tool definitions without leaking serialization into core schemas."""

    return [encode_tool(tool) for tool in tools]


def normalize_stop_reason(
    value: object,
    *,
    has_tool_calls: bool,
) -> StopReason:
    """Map Anthropic terminal values without treating unknown values as success."""

    if has_tool_calls or value == "tool_use":
        return StopReason.TOOL_CALL
    if value in {"end_turn", "stop_sequence"}:
        return StopReason.END_TURN
    if value in {"max_tokens", "model_context_window_exceeded"}:
        return StopReason.MAX_TOKENS
    if value == "refusal":
        return StopReason.CONTENT_FILTER
    return StopReason.UNKNOWN


def normalize_anthropic_error(
    error: APIError,
    *,
    provider: ProviderId,
) -> ProviderError:
    """Map Anthropic SDK failures to stable project-level categories."""

    label = _provider_label(provider)
    status_code: int | None = None
    retry_after: float | None = None
    if isinstance(error, APIStatusError):
        status_code = error.status_code
        retry_after = parse_retry_after(error.response.headers)

    if isinstance(error, AuthenticationError) or status_code in {401, 403}:
        return ProviderAuthenticationError(
            f"{label} API Key 无效，请检查当前 Provider 配置。",
            provider=provider,
            status_code=status_code,
            retry_after=retry_after,
        )
    if isinstance(error, RateLimitError) or status_code == 429:
        return ProviderRateLimitError(
            f"{label} 请求过于频繁，请稍后重试。",
            provider=provider,
            status_code=status_code,
            retry_after=retry_after,
        )
    if isinstance(error, APITimeoutError) or status_code == 408:
        return ProviderTimeoutError(
            f"{label} 请求超时，请检查网络后重试。",
            provider=provider,
            status_code=status_code,
            retry_after=retry_after,
        )
    if isinstance(error, APIConnectionError):
        return ProviderConnectionError(
            f"无法连接 {label} API，请检查网络和 API 地址。",
            provider=provider,
            status_code=status_code,
            retry_after=retry_after,
        )
    if status_code is not None and 500 <= status_code <= 599:
        return ProviderInternalError(
            f"{label} API 请求失败（HTTP {status_code}）。",
            provider=provider,
            status_code=status_code,
            retry_after=retry_after,
        )
    if status_code is not None and 400 <= status_code <= 499:
        return ProviderInvalidRequestError(
            f"{label} API 请求失败（HTTP {status_code}）。",
            provider=provider,
            status_code=status_code,
            retry_after=retry_after,
        )
    return ProviderProtocolError(
        f"{label} API 请求失败，请稍后重试。",
        provider=provider,
        status_code=status_code,
        retry_after=retry_after,
    )


def _provider_label(provider: ProviderId) -> str:
    return {
        ProviderId.DEEPSEEK: "DeepSeek",
        ProviderId.CLAUDE: "Claude",
        ProviderId.OPENAI: "OpenAI",
        ProviderId.OLLAMA: "Ollama",
        ProviderId.VLLM: "vLLM",
    }[provider]
