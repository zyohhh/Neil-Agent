"""OpenAI Responses wire encoding, decoding, and error normalization."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

from ..schemas import Message, ModelResponse, TokenUsage, ToolCall, ToolDefinition
from .base import ProviderId, ProviderTurnState, StopReason
from .errors import (
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderContextOverflowError,
    ProviderError,
    ProviderInternalError,
    ProviderInvalidRequestError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from .retry import parse_retry_after

OPENAI_STATE_SCHEMA_VERSION = 1
_CONTEXT_ERROR_CODES = frozenset(
    {
        "context_length_exceeded",
        "context_window_exceeded",
        "max_context_length_exceeded",
    }
)


@dataclass(frozen=True, slots=True)
class ParsedOutput:
    """Validated public output plus JSON-safe items for exact replay."""

    text: str
    tool_calls: tuple[ToolCall, ...]
    output_items: tuple[dict[str, object], ...]
    refused: bool


def encode_messages(
    messages: Sequence[Message],
    *,
    model: str,
    provider: ProviderId = ProviderId.OPENAI,
) -> list[dict[str, object]]:
    """Encode provider-neutral history as Responses input items."""

    try:
        encoded: list[dict[str, object]] = []
        for message in messages:
            if message.provider_state is not None:
                encoded.extend(
                    _private_output_items(message, model=model, provider=provider)
                )
                continue
            if message.content:
                encoded.append({"role": message.role, "content": message.content})
            encoded.extend(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": _encode_arguments(call.arguments),
                }
                for call in message.tool_calls
            )
            encoded.extend(
                {
                    "type": "function_call_output",
                    "call_id": result.tool_call_id,
                    "output": _encode_tool_output(
                        result.content,
                        is_error=result.is_error,
                    ),
                }
                for result in message.tool_results
            )
        return encoded
    except ProviderProtocolError as error:
        raise _rebind_protocol_error(error, provider) from error


def encode_tools(
    tools: Sequence[ToolDefinition],
    *,
    provider: ProviderId = ProviderId.OPENAI,
) -> list[dict[str, object]]:
    """Encode local tools using native Responses function-tool fields."""

    try:
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": _json_object(tool.input_schema, label="tool schema"),
                # Existing schemas need not satisfy OpenAI strict mode.
                "strict": False,
            }
            for tool in tools
        ]
    except ProviderProtocolError as error:
        raise _rebind_protocol_error(error, provider) from error


def parse_response(
    response: object,
    *,
    model: str,
    provider: ProviderId = ProviderId.OPENAI,
    preserve_state: bool = True,
    allow_reasoning: bool = True,
    allow_parallel_tool_calls: bool = True,
) -> ModelResponse:
    """Validate a terminal Responses object and normalize it for Agent."""

    try:
        return _parse_response(
            response,
            model=model,
            provider=provider,
            preserve_state=preserve_state,
            allow_reasoning=allow_reasoning,
            allow_parallel_tool_calls=allow_parallel_tool_calls,
        )
    except ProviderProtocolError as error:
        raise _rebind_protocol_error(error, provider) from error


def _parse_response(
    response: object,
    *,
    model: str,
    provider: ProviderId,
    preserve_state: bool,
    allow_reasoning: bool,
    allow_parallel_tool_calls: bool,
) -> ModelResponse:

    status = _optional_string(_field(response, "status"))
    if status in {"failed", "cancelled"}:
        raise _terminal_response_error(response, status=status, provider=provider)
    if status not in {"completed", "incomplete"}:
        raise ProviderProtocolError(
            "OpenAI Responses 返回了非终态响应。",
            provider=provider,
        )

    output = _required_sequence(_field(response, "output"), "output")
    parsed = parse_output_items(
        output,
        provider=provider,
        allow_reasoning=allow_reasoning,
    )
    if not parsed.text.strip() and not parsed.tool_calls:
        raise ProviderProtocolError(
            "OpenAI Responses 返回了空内容。",
            provider=provider,
        )

    usage_value = _field(response, "usage")
    usage = None if usage_value is None else to_token_usage(usage_value)
    stop_reason = _stop_reason(
        status,
        _field(response, "incomplete_details"),
        has_tool_calls=bool(parsed.tool_calls),
        refused=parsed.refused,
    )
    if not allow_parallel_tool_calls and len(parsed.tool_calls) > 1:
        raise ProviderProtocolError(
            "当前 Provider profile 不允许并行 function call。",
            provider=provider,
        )
    provider_state = None
    if preserve_state:
        provider_state = ProviderTurnState(
            provider=provider,
            model=model,
            schema_version=OPENAI_STATE_SCHEMA_VERSION,
            payload={"output_items": parsed.output_items},
        )
    return ModelResponse(
        content=parsed.text,
        tool_calls=parsed.tool_calls,
        usage=usage,
        stop_reason=stop_reason,
        provider_state=provider_state,
    )


def parse_output_items(
    items: Sequence[object],
    *,
    provider: ProviderId = ProviderId.OPENAI,
    allow_reasoning: bool = True,
) -> ParsedOutput:
    """Validate only output item kinds generated by this adapter's capabilities."""

    try:
        return _parse_output_items(
            items,
            provider=provider,
            allow_reasoning=allow_reasoning,
        )
    except ProviderProtocolError as error:
        raise _rebind_protocol_error(error, provider) from error


def _parse_output_items(
    items: Sequence[object],
    *,
    provider: ProviderId,
    allow_reasoning: bool,
) -> ParsedOutput:

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    replay_items: list[dict[str, object]] = []
    refused = False
    for item in items:
        try:
            item_type = _required_string(_field(item, "type"), "output item type")
            replay_item = _json_object(item, label="output item")
        except ProviderProtocolError as error:
            raise _rebind_protocol_error(error, provider) from error
        if item_type == "message":
            _validate_output_message(item, provider=provider)
            for part in _required_sequence(_field(item, "content"), "message content"):
                part_type = _required_string(
                    _field(part, "type"),
                    "message content type",
                )
                if part_type == "output_text":
                    text_parts.append(
                        _required_string(
                            _field(part, "text"),
                            "output text",
                            allow_empty=True,
                        )
                    )
                elif part_type == "refusal":
                    refused = True
                    text_parts.append(
                        _required_string(_field(part, "refusal"), "refusal text")
                    )
                else:
                    raise ProviderProtocolError(
                        f"OpenAI message 包含未支持的 content part：{part_type}。",
                        provider=provider,
                    )
        elif item_type == "function_call":
            status = _optional_string(_field(item, "status"))
            if status not in {None, "completed"}:
                raise ProviderProtocolError(
                    "OpenAI 返回了未完成的 function call。",
                    provider=provider,
                )
            _validate_direct_caller(item, provider=provider)
            arguments_text = _required_string(
                _field(item, "arguments"),
                "function arguments",
            )
            try:
                arguments = json.loads(
                    arguments_text,
                    object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant,
                )
            except (json.JSONDecodeError, ValueError) as error:
                raise ProviderProtocolError(
                    "OpenAI function call 参数不是完整 JSON。",
                    provider=provider,
                ) from error
            if not isinstance(arguments, dict):
                raise ProviderProtocolError(
                    "OpenAI function call 参数必须是 JSON 对象。",
                    provider=provider,
                )
            tool_calls.append(
                ToolCall(
                    id=_required_string(_field(item, "call_id"), "function call ID"),
                    name=_required_string(_field(item, "name"), "function name"),
                    arguments=cast(dict[str, Any], arguments),
                )
            )
        elif item_type == "reasoning":
            if not allow_reasoning:
                raise ProviderProtocolError(
                    "当前 Provider profile 不接受 reasoning output item。",
                    provider=provider,
                )
            _required_string(_field(item, "id"), "reasoning item ID")
            _required_sequence(_field(item, "summary"), "reasoning summary")
            _required_string(
                _field(item, "encrypted_content"),
                "encrypted reasoning content",
            )
        else:
            raise ProviderProtocolError(
                f"OpenAI Responses 返回了未支持的 output item：{item_type}。",
                provider=provider,
            )
        replay_items.append(replay_item)

    call_ids = tuple(call.id for call in tool_calls)
    if len(call_ids) != len(set(call_ids)):
        raise ProviderProtocolError(
            "OpenAI Responses 返回了重复的 function call ID。",
            provider=provider,
        )
    return ParsedOutput(
        text="".join(text_parts),
        tool_calls=tuple(tool_calls),
        output_items=tuple(replay_items),
        refused=refused,
    )


def to_token_usage(usage: object) -> TokenUsage:
    """Copy stable token counts, including OpenAI cache read/write counters."""

    details = _field(usage, "input_tokens_details")
    return TokenUsage(
        input_tokens=_token_count(usage, "input_tokens"),
        output_tokens=_token_count(usage, "output_tokens"),
        cache_creation_input_tokens=(
            _token_count(details, "cache_write_tokens") if details is not None else 0
        ),
        cache_read_input_tokens=(
            _token_count(details, "cached_tokens") if details is not None else 0
        ),
    )


def normalize_openai_error(
    error: APIError,
    *,
    provider: ProviderId = ProviderId.OPENAI,
) -> ProviderError:
    """Map OpenAI SDK exceptions to stable, secret-safe project errors."""

    status_code: int | None = None
    retry_after: float | None = None
    if isinstance(error, APIStatusError):
        status_code = error.status_code
        retry_after = parse_retry_after(error.response.headers)
    code = _sdk_error_code(error)

    label = _provider_label(provider)
    if isinstance(error, AuthenticationError) or status_code in {401, 403}:
        return ProviderAuthenticationError(
            f"{label} API 鉴权失败，请检查当前 Provider 配置。",
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
            f"{label} 请求超时，请检查服务状态后重试。",
            provider=provider,
            status_code=status_code,
            retry_after=retry_after,
        )
    if isinstance(error, APIConnectionError):
        return ProviderConnectionError(
            f"无法连接 {label} API，请检查服务和 API 地址。",
            provider=provider,
            status_code=status_code,
            retry_after=retry_after,
        )
    if code in _CONTEXT_ERROR_CODES:
        return ProviderContextOverflowError(
            f"{label} 请求超出模型上下文窗口。",
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
    if isinstance(error, BadRequestError) or (
        status_code is not None and 400 <= status_code <= 499
    ):
        return ProviderInvalidRequestError(
            f"{label} API 请求失败（HTTP {status_code or 400}）。",
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


def normalize_stream_error(
    code: object,
    *,
    provider: ProviderId = ProviderId.OPENAI,
) -> ProviderError:
    """Normalize an SSE error event without copying its untrusted message."""

    value = code if isinstance(code, str) else None
    label = _provider_label(provider)
    if value == "rate_limit_exceeded":
        return ProviderRateLimitError(
            f"{label} 流式请求受到限流，请稍后重试。",
            provider=provider,
        )
    if value in _CONTEXT_ERROR_CODES:
        return ProviderContextOverflowError(
            f"{label} 请求超出模型上下文窗口。",
            provider=provider,
        )
    if value == "server_error":
        return ProviderInternalError(
            f"{label} 流式请求遇到服务端错误。",
            provider=provider,
        )
    return ProviderProtocolError(
        f"{label} 流式请求返回错误事件。",
        provider=provider,
    )


def _private_output_items(
    message: Message,
    *,
    model: str,
    provider: ProviderId,
) -> list[dict[str, object]]:
    state = message.provider_state
    if state is None:
        return []
    if not state.belongs_to(provider, model):
        raise ProviderProtocolError(
            "Provider private state cannot be replayed across providers or models.",
            provider=provider,
        )
    if state.schema_version != OPENAI_STATE_SCHEMA_VERSION or set(state.payload) != {
        "output_items"
    }:
        raise ProviderProtocolError(
            "OpenAI private state has an unsupported schema.",
            provider=provider,
        )
    raw_items = state.payload["output_items"]
    if not isinstance(raw_items, (list, tuple)):
        raise ProviderProtocolError(
            "OpenAI private state has an invalid output item collection.",
            provider=provider,
        )
    parsed = parse_output_items(raw_items, provider=provider)
    public_tools = tuple(
        (call.id, call.name, call.arguments) for call in message.tool_calls
    )
    private_tools = tuple(
        (call.id, call.name, call.arguments) for call in parsed.tool_calls
    )
    if parsed.text != message.content or private_tools != public_tools:
        raise ProviderProtocolError(
            "OpenAI private state does not match the public assistant message.",
            provider=provider,
        )
    return [dict(item) for item in parsed.output_items]


def _validate_output_message(item: object, *, provider: ProviderId) -> None:
    _required_string(_field(item, "id"), "message item ID")
    if _field(item, "role") != "assistant":
        raise ProviderProtocolError(
            "OpenAI output message 具有无效角色。",
            provider=provider,
        )
    if _field(item, "status") not in {"completed", "incomplete"}:
        raise ProviderProtocolError(
            "OpenAI output message 具有无效状态。",
            provider=provider,
        )


def _validate_direct_caller(item: object, *, provider: ProviderId) -> None:
    namespace = _field(item, "namespace")
    if namespace not in {None, ""}:
        raise ProviderProtocolError(
            "OpenAI 返回了未配置命名空间的 function call。",
            provider=provider,
        )
    caller = _field(item, "caller")
    if caller is not None and _field(caller, "type") != "direct":
        raise ProviderProtocolError(
            "OpenAI 返回了非直接 function call。",
            provider=provider,
        )


def _stop_reason(
    status: str,
    incomplete_details: object,
    *,
    has_tool_calls: bool,
    refused: bool,
) -> StopReason:
    if has_tool_calls:
        return StopReason.TOOL_CALL
    if refused:
        return StopReason.CONTENT_FILTER
    if status == "completed":
        return StopReason.END_TURN
    reason = _field(incomplete_details, "reason")
    if reason == "max_output_tokens":
        return StopReason.MAX_TOKENS
    if reason == "content_filter":
        return StopReason.CONTENT_FILTER
    return StopReason.UNKNOWN


def _terminal_response_error(
    response: object,
    *,
    status: str,
    provider: ProviderId,
) -> ProviderError:
    error = _field(response, "error")
    code = _field(error, "code")
    if code == "rate_limit_exceeded":
        return ProviderRateLimitError(
            "OpenAI 响应因限流失败。",
            provider=provider,
        )
    if code == "server_error":
        return ProviderInternalError(
            "OpenAI 响应因服务端错误失败。",
            provider=provider,
        )
    return ProviderProtocolError(
        f"OpenAI Responses 以 {status} 状态终止。",
        provider=provider,
    )


def _encode_arguments(arguments: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            arguments,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ProviderProtocolError(
            "工具参数无法编码为 OpenAI JSON。",
            provider=ProviderId.OPENAI,
        ) from error


def _encode_tool_output(content: str, *, is_error: bool) -> str:
    if not is_error:
        return content
    return f"Tool execution failed:\n{content}"


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")


def _sdk_error_code(error: APIError) -> str | None:
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    body = getattr(error, "body", None)
    if not isinstance(body, Mapping):
        return None
    direct = body.get("code")
    if isinstance(direct, str):
        return direct
    nested = body.get("error")
    if isinstance(nested, Mapping) and isinstance(nested.get("code"), str):
        return cast(str, nested["code"])
    return None


def _json_object(value: object, *, label: str) -> dict[str, object]:
    if isinstance(value, Mapping):
        candidate: object = _thaw_json(value)
    else:
        dump = getattr(value, "model_dump", None)
        if not callable(dump):
            raise ProviderProtocolError(
                f"OpenAI {label} 不是可序列化对象。",
                provider=ProviderId.OPENAI,
            )
        candidate = dump(mode="json", exclude_none=True)
    try:
        copied = json.loads(json.dumps(candidate, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ProviderProtocolError(
            f"OpenAI {label} 不是合法 JSON。",
            provider=ProviderId.OPENAI,
        ) from error
    if not isinstance(copied, dict) or not all(isinstance(key, str) for key in copied):
        raise ProviderProtocolError(
            f"OpenAI {label} 必须是 JSON 对象。",
            provider=ProviderId.OPENAI,
        )
    return cast(dict[str, object], copied)


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _field(value: object, name: str) -> object:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _required_sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProviderProtocolError(
            f"OpenAI {label} 不是有效集合。",
            provider=ProviderId.OPENAI,
        )
    return value


def _required_string(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ProviderProtocolError(
            f"OpenAI {label} 不是有效字符串。",
            provider=ProviderId.OPENAI,
        )
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _token_count(value: object, field: str) -> int:
    count = _field(value, field)
    return count if isinstance(count, int) and count >= 0 else 0


def _provider_label(provider: ProviderId) -> str:
    return {
        ProviderId.OPENAI: "OpenAI",
        ProviderId.OLLAMA: "Ollama",
        ProviderId.VLLM: "vLLM",
        ProviderId.DEEPSEEK: "DeepSeek",
        ProviderId.CLAUDE: "Claude",
    }[provider]


def _rebind_protocol_error(
    error: ProviderProtocolError,
    provider: ProviderId,
) -> ProviderProtocolError:
    if error.provider is provider:
        return error
    return ProviderProtocolError(
        str(error),
        provider=provider,
        status_code=error.status_code,
        retry_after=error.retry_after,
    )
