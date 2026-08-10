"""DeepSeek model integration through its Anthropic-compatible API."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from time import sleep

from anthropic import (
    APIError,
    Anthropic,
)
from anthropic.types import (
    ContentBlock,
    Message as AnthropicMessage,
    MessageParam,
    ThinkingConfigParam,
    ToolParam,
)

from .config import Settings, get_settings
from .providers.anthropic_messages import (
    encode_messages,
    encode_tools,
    normalize_anthropic_error,
    normalize_stop_reason,
)
from .providers.base import (
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderId,
    WireProtocol,
)
from .providers.errors import (
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderError,
    ProviderInternalError,
    ProviderNotImplementedError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from .providers.retry import RetryPolicy
from .schemas import (
    ActivityEvent,
    Message,
    ModelResponse,
    ThinkingContent,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)

RetryHandler = Callable[[ActivityEvent], None]
Sleeper = Callable[[float], None]
DEEPSEEK_DESCRIPTOR = ProviderDescriptor(
    provider=ProviderId.DEEPSEEK,
    display_name="DeepSeek",
    wire_protocol=WireProtocol.ANTHROPIC_MESSAGES,
    capabilities=ProviderCapabilities(
        streaming=True,
        tool_calling=True,
        parallel_tool_calls=False,
        reasoning_state=True,
        structured_output=False,
        usage_reporting=True,
        prompt_caching=False,
    ),
)


class LLMClient:
    """Small wrapper around the Anthropic SDK configured for DeepSeek."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: Anthropic | None = None,
        retry_handler: RetryHandler | None = None,
        sleeper: Sleeper = sleep,
    ) -> None:
        self.settings = settings or get_settings()
        if self.settings.llm_provider is not ProviderId.DEEPSEEK:
            raise ProviderNotImplementedError(
                f"Provider '{self.settings.llm_provider.value}' "
                "不能通过 DeepSeek 兼容入口启动。",
                provider=self.settings.llm_provider,
            )
        api_key = self.settings.selected_api_key
        if api_key is None:
            raise ProviderAuthenticationError(
                "未配置 DeepSeek API Key。",
                provider=ProviderId.DEEPSEEK,
            )
        base_url = self.settings.selected_base_url
        if base_url is None:
            raise ProviderProtocolError(
                "未配置 DeepSeek API 地址。",
                provider=ProviderId.DEEPSEEK,
            )
        self._client = client or Anthropic(
            api_key=api_key.get_secret_value(),
            base_url=str(base_url).rstrip("/"),
            timeout=self.settings.request_timeout,
            max_retries=0,
        )
        self._retry_policy = RetryPolicy(
            max_retries=self.settings.max_retries,
            base_delay=self.settings.retry_base_delay,
            max_delay=self.settings.retry_max_delay,
        )
        self._retry_handler = retry_handler
        self._sleeper = sleeper
        self._last_usage: TokenUsage | None = None

    def replace_retry_handler(
        self,
        handler: RetryHandler | None,
    ) -> RetryHandler | None:
        """Replace terminal retry output between model requests."""

        previous = self._retry_handler
        self._retry_handler = handler
        return previous

    @property
    def last_usage(self) -> TokenUsage | None:
        """Return usage from the most recently completed SDK request."""

        return self._last_usage

    @property
    def descriptor(self) -> ProviderDescriptor:
        """Return safe metadata and the capabilities of this adapter."""

        return DEEPSEEK_DESCRIPTOR

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        """Return one complete model response without streaming."""

        self._ensure_messages(messages)
        self._last_usage = None
        retries_done = 0
        while True:
            try:
                response = self._client.messages.create(
                    model=self.settings.selected_model,
                    max_tokens=self.settings.max_tokens,
                    system=system_prompt,
                    messages=self._to_api_messages(messages),
                    thinking=self._thinking_config(),
                )
                break
            except APIError as sdk_error:
                error = normalize_anthropic_error(
                    sdk_error,
                    provider=ProviderId.DEEPSEEK,
                )
                if not self._retry_policy.can_retry(error, retries_done):
                    raise error from sdk_error
                retries_done += 1
                self._wait_for_retry(error, retries_done)

        sdk_usage = getattr(response, "usage", None)
        if sdk_usage is not None:
            self._last_usage = self._to_token_usage(sdk_usage)
        return self._extract_text(response.content)

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

        retries_done = 0
        emitted_text = False
        while True:
            try:
                with self._client.messages.stream(
                    model=self.settings.selected_model,
                    max_tokens=self.settings.max_tokens,
                    system=system_prompt,
                    messages=self._to_api_messages(messages),
                    thinking=self._thinking_config(),
                    tools=self._to_api_tools(tools),
                ) as stream:
                    for text in stream.text_stream:
                        if text:
                            emitted_text = True
                            yield text
                    final_message = stream.get_final_message()
                break
            except APIError as sdk_error:
                error = normalize_anthropic_error(
                    sdk_error,
                    provider=ProviderId.DEEPSEEK,
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

    @staticmethod
    def _ensure_messages(messages: Sequence[Message]) -> None:
        if not messages:
            raise ValueError("at least one message is required")

    @staticmethod
    def _to_api_messages(messages: Sequence[Message]) -> list[MessageParam]:
        return encode_messages(messages)

    @staticmethod
    def _to_api_tools(tools: Sequence[ToolDefinition]) -> list[ToolParam]:
        return encode_tools(tools)

    def _thinking_config(self) -> ThinkingConfigParam:
        if self.settings.thinking_enabled:
            # DeepSeek accepts the Anthropic field but ignores budget_tokens.
            return {"type": "enabled", "budget_tokens": 1024}
        return {"type": "disabled"}

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

    @staticmethod
    def _retry_reason(error: ProviderError) -> str:
        if isinstance(error, ProviderRateLimitError):
            return "DeepSeek 限流"
        if isinstance(error, ProviderTimeoutError):
            return "请求超时"
        if isinstance(error, ProviderConnectionError):
            return "连接中断"
        if isinstance(error, ProviderInternalError) and error.status_code is not None:
            return f"服务端 HTTP {error.status_code}"
        return "Provider 临时错误"

    @staticmethod
    def _extract_text(content: Iterable[ContentBlock]) -> str:
        text = "".join(block.text for block in content if block.type == "text")
        if not text.strip():
            raise ProviderProtocolError(
                "模型返回了空内容，请重新尝试。",
                provider=ProviderId.DEEPSEEK,
            )
        return text

    @staticmethod
    def _to_model_response(message: AnthropicMessage) -> ModelResponse:
        blocks = list(message.content)
        text = "".join(block.text for block in blocks if block.type == "text")
        tool_calls = tuple(
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in blocks
            if block.type == "tool_use"
        )
        if not text.strip() and not tool_calls:
            raise ProviderProtocolError(
                "模型返回了空内容，请重新尝试。",
                provider=ProviderId.DEEPSEEK,
            )

        thinking: tuple[ThinkingContent, ...] = ()
        if tool_calls:
            thinking = tuple(
                ThinkingContent(
                    thinking=block.thinking,
                    signature=block.signature,
                )
                for block in blocks
                if block.type == "thinking"
            )
        sdk_usage = getattr(message, "usage", None)
        return ModelResponse(
            content=text,
            thinking=thinking,
            tool_calls=tool_calls,
            usage=(
                LLMClient._to_token_usage(sdk_usage) if sdk_usage is not None else None
            ),
            stop_reason=normalize_stop_reason(
                getattr(message, "stop_reason", None),
                has_tool_calls=bool(tool_calls),
            ),
        )

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
