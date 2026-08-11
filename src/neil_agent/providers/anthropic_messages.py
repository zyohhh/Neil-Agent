"""Anthropic Messages serialization and SDK error normalization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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


def encode_message(
    message: Message,
    *,
    provider: ProviderId | None = None,
    model: str | None = None,
) -> MessageParam:
    """Encode one provider-neutral message as Anthropic content blocks."""

    if (
        not message.thinking
        and message.provider_state is None
        and not message.tool_calls
        and not message.tool_results
    ):
        return cast(MessageParam, {"role": message.role, "content": message.content})

    blocks: list[dict[str, object]] = []
    if message.provider_state is not None:
        if provider is None or model is None:
            raise ProviderProtocolError(
                "Anthropic private state requires a target provider and model.",
                provider=message.provider_state.provider,
            )
        if not message.provider_state.belongs_to(provider, model):
            raise ProviderProtocolError(
                "Provider private state cannot be replayed across providers or models.",
                provider=provider,
            )
        blocks.extend(_private_content_blocks(message))
        return cast(MessageParam, {"role": message.role, "content": blocks})
    else:
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


def encode_messages(
    messages: Sequence[Message],
    *,
    provider: ProviderId | None = None,
    model: str | None = None,
) -> list[MessageParam]:
    """Encode a complete request history for Anthropic Messages."""

    return [
        encode_message(message, provider=provider, model=model) for message in messages
    ]


def _private_content_blocks(message: Message) -> list[dict[str, object]]:
    state = message.provider_state
    if state is None:
        return []
    raw_blocks = state.payload.get("content_blocks")
    if not isinstance(raw_blocks, (list, tuple)):
        raise ProviderProtocolError(
            "Anthropic private state has an invalid content block collection.",
            provider=state.provider,
        )
    blocks: list[dict[str, object]] = []
    text_parts: list[str] = []
    thinking_parts: list[tuple[str, str]] = []
    tool_parts: list[tuple[str, str, dict[str, object]]] = []
    for raw_block in raw_blocks:
        if not isinstance(raw_block, Mapping):
            raise ProviderProtocolError(
                "Anthropic private state contains an invalid content block.",
                provider=state.provider,
            )
        block_type = raw_block.get("type")
        if block_type == "text":
            required = {"type", "text"}
            _require_exact_string_fields(raw_block, required, state.provider)
            text_parts.append(cast(str, raw_block["text"]))
            block = dict(raw_block)
        elif block_type == "thinking":
            required = {"type", "thinking", "signature"}
            _require_exact_string_fields(raw_block, required, state.provider)
            thinking_parts.append(
                (
                    cast(str, raw_block["thinking"]),
                    cast(str, raw_block["signature"]),
                )
            )
            block = dict(raw_block)
        elif block_type == "redacted_thinking":
            required = {"type", "data"}
            _require_exact_string_fields(raw_block, required, state.provider)
            block = dict(raw_block)
        elif block_type == "tool_use":
            required = {"type", "id", "name", "input"}
            if set(raw_block) != required:
                raise ProviderProtocolError(
                    "Anthropic private state contains a malformed content block.",
                    provider=state.provider,
                )
            call_id = raw_block["id"]
            name = raw_block["name"]
            arguments = raw_block["input"]
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(name, str)
                or not name
                or not isinstance(arguments, Mapping)
            ):
                raise ProviderProtocolError(
                    "Anthropic private state contains a malformed tool block.",
                    provider=state.provider,
                )
            copied_arguments = cast(dict[str, object], _json_copy(arguments))
            tool_parts.append((call_id, name, copied_arguments))
            block = {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": copied_arguments,
            }
        else:
            raise ProviderProtocolError(
                "Anthropic private state contains an unsupported content block.",
                provider=state.provider,
            )
        blocks.append(block)

    public_thinking = [(item.thinking, item.signature) for item in message.thinking]
    public_tools = [(call.id, call.name, call.arguments) for call in message.tool_calls]
    if (
        "".join(text_parts) != message.content
        or thinking_parts != public_thinking
        or tool_parts != public_tools
    ):
        raise ProviderProtocolError(
            "Anthropic private state does not match the public assistant message.",
            provider=state.provider,
        )
    return blocks


def _require_exact_string_fields(
    block: Mapping[str, object],
    required: set[str],
    provider: ProviderId,
) -> None:
    if set(block) != required or not all(
        isinstance(block[name], str) for name in required
    ):
        raise ProviderProtocolError(
            "Anthropic private state contains a malformed content block.",
            provider=provider,
        )
    for name in required - {"thinking", "text"}:
        if not cast(str, block[name]):
            raise ProviderProtocolError(
                "Anthropic private state contains an empty required field.",
                provider=provider,
            )


def _json_copy(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_copy(item) for item in value]
    return value


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
