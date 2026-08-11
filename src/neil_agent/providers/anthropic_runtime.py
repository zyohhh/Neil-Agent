"""Shared runtime for providers that implement Anthropic Messages."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from time import sleep
from typing import Any

from anthropic import APIError, Anthropic
from anthropic.types import Message as AnthropicMessage
from anthropic.types import ThinkingConfigParam

from ..config import Settings, get_settings
from ..schemas import (
    ActivityEvent,
    Message,
    ModelResponse,
    ThinkingContent,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from .anthropic_messages import (
    encode_messages,
    encode_tools,
    normalize_anthropic_error,
    normalize_stop_reason,
)
from .base import ProviderDescriptor, ProviderId, ProviderTurnState
from .errors import (
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderError,
    ProviderInternalError,
    ProviderNotImplementedError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from .retry import RetryPolicy

RetryHandler = Callable[[ActivityEvent], None]
Sleeper = Callable[[float], None]
AnthropicClientFactory = Callable[..., Anthropic]


class AnthropicMessagesProvider:
    """Auditable request, response, streaming, and retry core."""

    provider_id: ProviderId
    provider_descriptor: ProviderDescriptor

    def __init__(
        self,
        settings: Settings | None = None,
        client: Anthropic | None = None,
        retry_handler: RetryHandler | None = None,
        sleeper: Sleeper = sleep,
        *,
        client_factory: AnthropicClientFactory | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if self.settings.llm_provider is not self.provider_id:
            entry_name = (
                "DeepSeek 兼容入口"
                if self.provider_id is ProviderId.DEEPSEEK
                else f"{self.descriptor.display_name} adapter"
            )
            raise ProviderNotImplementedError(
                f"Provider '{self.settings.llm_provider.value}' cannot be started "
                f"through the {entry_name}.",
                provider=self.settings.llm_provider,
            )

        api_key = self.settings.selected_api_key
        if api_key is None:
            raise ProviderAuthenticationError(
                f"未配置 {self.descriptor.display_name} API Key。",
                provider=self.provider_id,
            )

        factory = client_factory or Anthropic
        if client is None:
            client_kwargs: dict[str, Any] = {
                "api_key": api_key.get_secret_value(),
                "timeout": self.settings.request_timeout,
                "max_retries": 0,
            }
            base_url = self._base_url()
            if base_url is not None:
                client_kwargs["base_url"] = base_url
            client = factory(**client_kwargs)
        self._client = client
        self._retry_policy = RetryPolicy(
            max_retries=self.settings.max_retries,
            base_delay=self.settings.retry_base_delay,
            max_delay=self.settings.retry_max_delay,
        )
        self._retry_handler = retry_handler
        self._sleeper = sleeper
        self._last_usage: TokenUsage | None = None

    @property
    def descriptor(self) -> ProviderDescriptor:
        """Return safe metadata and the capabilities of this adapter."""

        return self.provider_descriptor

    @property
    def last_usage(self) -> TokenUsage | None:
        """Return usage from the most recently completed SDK request."""

        return self._last_usage

    def replace_retry_handler(
        self,
        handler: RetryHandler | None,
    ) -> RetryHandler | None:
        """Replace terminal retry output between model requests."""

        previous = self._retry_handler
        self._retry_handler = handler
        return previous

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        """Return one complete model response without streaming."""

        self._ensure_messages(messages)
        self._last_usage = None
        request = self._request_kwargs(messages, system_prompt=system_prompt)
        retries_done = 0
        while True:
            try:
                response = self._client.messages.create(**request)
                break
            except APIError as sdk_error:
                error = normalize_anthropic_error(
                    sdk_error,
                    provider=self.provider_id,
                )
                if not self._retry_policy.can_retry(error, retries_done):
                    raise error from sdk_error
                retries_done += 1
                self._wait_for_retry(error, retries_done)

        raw_content = getattr(response, "content", None)
        if not isinstance(raw_content, Sequence) or isinstance(
            raw_content, (str, bytes)
        ):
            raise ProviderProtocolError(
                "模型返回了无效的 content 集合。",
                provider=self.provider_id,
            )
        text = self._extract_text(raw_content)
        sdk_usage = getattr(response, "usage", None)
        if sdk_usage is not None:
            self._last_usage = self._to_token_usage(sdk_usage)
        return text

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        """Yield text fragments followed by one accumulated response event."""

        self._ensure_messages(messages)
        self._last_usage = None
        request = self._request_kwargs(
            messages,
            system_prompt=system_prompt,
            tools=tools,
        )
        retries_done = 0
        emitted_text = False
        while True:
            try:
                with self._client.messages.stream(**request) as stream:
                    for text in stream.text_stream:
                        if text:
                            emitted_text = True
                            yield text
                    final_message = stream.get_final_message()
                break
            except APIError as sdk_error:
                error = normalize_anthropic_error(
                    sdk_error,
                    provider=self.provider_id,
                )
                if not self._retry_policy.can_retry(
                    error,
                    retries_done,
                    output_started=emitted_text,
                ):
                    raise error from sdk_error
                retries_done += 1
                self._wait_for_retry(error, retries_done)

        model_response = self._to_model_response(final_message)
        self._last_usage = model_response.usage
        yield model_response

    def _request_kwargs(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.settings.selected_model,
            "max_tokens": self.settings.max_tokens,
            "system": system_prompt,
            "messages": encode_messages(
                messages,
                provider=self.provider_id,
                model=self.settings.selected_model,
            ),
        }
        thinking = self._thinking_config()
        if thinking is not None:
            request["thinking"] = thinking
        if tools is not None:
            request["tools"] = encode_tools(tools)
        return request

    def _base_url(self) -> str | None:
        selected = self.settings.selected_base_url
        return str(selected).rstrip("/") if selected is not None else None

    def _thinking_config(self) -> ThinkingConfigParam | None:
        raise NotImplementedError

    @staticmethod
    def _ensure_messages(messages: Sequence[Message]) -> None:
        if not messages:
            raise ValueError("at least one message is required")

    def _wait_for_retry(self, error: ProviderError, retry_number: int) -> None:
        delay = self._retry_policy.delay(error, retry_number)
        self._emit_retry_activity(
            "模型请求暂时失败，等待重试",
            error,
            retry_number,
            delay,
        )
        self._sleeper(delay)
        if self._retry_handler is not None:
            self._retry_handler(
                ActivityEvent(
                    status="running",
                    message="重试模型请求",
                    details=(f"重试：{retry_number}/{self.settings.max_retries}",),
                )
            )

    def _emit_retry_activity(
        self,
        message: str,
        error: ProviderError,
        retry_number: int,
        delay: float,
    ) -> None:
        if self._retry_handler is None:
            return
        self._retry_handler(
            ActivityEvent(
                status="running",
                message=message,
                details=(
                    f"原因：{self._retry_reason(error)}",
                    f"重试：{retry_number}/{self.settings.max_retries}",
                    f"等待：{delay:g} 秒",
                ),
            )
        )

    def _retry_reason(self, error: ProviderError) -> str:
        if isinstance(error, ProviderRateLimitError):
            return f"{self.descriptor.display_name} 限流"
        if isinstance(error, ProviderTimeoutError):
            return "请求超时"
        if isinstance(error, ProviderConnectionError):
            return "连接中断"
        if isinstance(error, ProviderInternalError) and error.status_code is not None:
            return f"服务端 HTTP {error.status_code}"
        return "Provider 临时错误"

    def _extract_text(self, content: Iterable[object]) -> str:
        blocks = list(content)
        allowed_types = {"text", "thinking", "redacted_thinking"}
        unexpected_types = {
            str(getattr(block, "type", None))
            for block in blocks
            if getattr(block, "type", None) not in allowed_types
        }
        if unexpected_types:
            values = ", ".join(sorted(unexpected_types))
            raise ProviderProtocolError(
                f"非流式完成返回了未支持的 content block：{values}。",
                provider=self.provider_id,
            )
        text = "".join(
            self._block_string(block, "text", allow_empty=True)
            for block in blocks
            if getattr(block, "type", None) == "text"
        )
        if not text.strip():
            raise ProviderProtocolError(
                "模型返回了空内容，请重新尝试。",
                provider=self.provider_id,
            )
        return text

    def _to_model_response(self, message: AnthropicMessage) -> ModelResponse:
        raw_content = getattr(message, "content", None)
        if not isinstance(raw_content, Sequence) or isinstance(
            raw_content, (str, bytes)
        ):
            raise ProviderProtocolError(
                "模型返回了无效的 content 集合。",
                provider=self.provider_id,
            )
        blocks = list(raw_content)
        known_types = {"text", "thinking", "redacted_thinking", "tool_use"}
        unknown_types = {
            str(getattr(block, "type", None))
            for block in blocks
            if getattr(block, "type", None) not in known_types
        }
        if unknown_types:
            values = ", ".join(sorted(unknown_types))
            raise ProviderProtocolError(
                f"模型返回了未支持的 Anthropic content block：{values}。",
                provider=self.provider_id,
            )

        text_parts: list[str] = []
        tool_calls_list: list[ToolCall] = []
        replay_blocks: list[dict[str, object]] = []
        thinking: list[ThinkingContent] = []
        for block in blocks:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                value = self._block_string(block, "text", allow_empty=True)
                text_parts.append(value)
                replay_blocks.append({"type": "text", "text": value})
            elif block_type == "tool_use":
                raw_input = getattr(block, "input", None)
                if not isinstance(raw_input, Mapping):
                    raise ProviderProtocolError(
                        "模型返回了无效的工具参数对象。",
                        provider=self.provider_id,
                    )
                call = ToolCall(
                    id=self._block_string(block, "id"),
                    name=self._block_string(block, "name"),
                    arguments=dict(raw_input),
                )
                tool_calls_list.append(call)
                replay_blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            elif block_type == "thinking":
                thinking_text = self._block_string(
                    block,
                    "thinking",
                    allow_empty=True,
                )
                signature = self._block_string(block, "signature")
                replay_blocks.append(
                    {
                        "type": "thinking",
                        "thinking": thinking_text,
                        "signature": signature,
                    }
                )
                thinking.append(
                    ThinkingContent(
                        thinking=thinking_text,
                        signature=signature,
                    )
                )
            elif block_type == "redacted_thinking":
                replay_blocks.append(
                    {
                        "type": "redacted_thinking",
                        "data": self._block_string(block, "data"),
                    }
                )
        text = "".join(text_parts)
        tool_calls = tuple(tool_calls_list)
        tool_call_ids = tuple(call.id for call in tool_calls)
        if len(tool_call_ids) != len(set(tool_call_ids)):
            raise ProviderProtocolError(
                "模型返回了重复的工具调用 ID。",
                provider=self.provider_id,
            )
        if not text.strip() and not tool_calls:
            raise ProviderProtocolError(
                "模型返回了空内容，请重新尝试。",
                provider=self.provider_id,
            )

        provider_state = None
        if tool_calls:
            provider_state = ProviderTurnState(
                provider=self.provider_id,
                model=self.settings.selected_model,
                schema_version=1,
                payload={"content_blocks": tuple(replay_blocks)},
            )
        sdk_usage = getattr(message, "usage", None)
        return ModelResponse(
            content=text,
            thinking=tuple(thinking),
            tool_calls=tool_calls,
            usage=(self._to_token_usage(sdk_usage) if sdk_usage is not None else None),
            stop_reason=normalize_stop_reason(
                getattr(message, "stop_reason", None),
                has_tool_calls=bool(tool_calls),
            ),
            provider_state=provider_state,
        )

    def _block_string(
        self,
        block: object,
        field: str,
        *,
        allow_empty: bool = False,
    ) -> str:
        value = getattr(block, field, None)
        if not isinstance(value, str) or (not allow_empty and not value):
            raise ProviderProtocolError(
                f"Anthropic content block 缺少有效的 {field} 字段。",
                provider=self.provider_id,
            )
        return value

    @staticmethod
    def _to_token_usage(usage: object) -> TokenUsage:
        """Copy only stable numeric fields from the SDK usage object."""

        def token_count(name: str) -> int:
            value = getattr(usage, name, 0)
            return value if isinstance(value, int) and value >= 0 else 0

        return TokenUsage(
            input_tokens=token_count("input_tokens"),
            output_tokens=token_count("output_tokens"),
            cache_creation_input_tokens=token_count("cache_creation_input_tokens"),
            cache_read_input_tokens=token_count("cache_read_input_tokens"),
        )
