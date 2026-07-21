"""DeepSeek model integration through its Anthropic-compatible API."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from time import sleep
from typing import cast

from anthropic import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    Anthropic,
    AuthenticationError,
    RateLimitError,
)
from anthropic.types import ContentBlock, MessageParam, ThinkingConfigParam, ToolParam

from .config import Settings, get_settings
from .errors import LLMError
from .schemas import (
    ActivityEvent,
    Message,
    ModelResponse,
    ThinkingContent,
    ToolCall,
    ToolDefinition,
)

RetryHandler = Callable[[ActivityEvent], None]
Sleeper = Callable[[float], None]


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
        self._client = client or Anthropic(
            api_key=self.settings.deepseek_api_key.get_secret_value(),
            base_url=str(self.settings.deepseek_base_url).rstrip("/"),
            timeout=self.settings.request_timeout,
            max_retries=0,
        )
        self._retry_handler = retry_handler
        self._sleeper = sleeper

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        """Return one complete model response without streaming."""

        self._ensure_messages(messages)
        retries_done = 0
        while True:
            try:
                response = self._client.messages.create(
                    model=self.settings.deepseek_model,
                    max_tokens=self.settings.max_tokens,
                    system=system_prompt,
                    messages=self._to_api_messages(messages),
                    thinking=self._thinking_config(),
                )
                break
            except APIError as error:
                if not self._can_retry(error, retries_done):
                    raise self._friendly_error(error) from error
                retries_done += 1
                self._wait_for_retry(error, retries_done)

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

        retries_done = 0
        emitted_text = False
        while True:
            try:
                with self._client.messages.stream(
                    model=self.settings.deepseek_model,
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
            except APIError as error:
                if emitted_text or not self._can_retry(error, retries_done):
                    raise self._friendly_error(error) from error
                retries_done += 1
                self._wait_for_retry(error, retries_done)

        yield self._to_model_response(final_message.content)

    @staticmethod
    def _ensure_messages(messages: Sequence[Message]) -> None:
        if not messages:
            raise ValueError("at least one message is required")

    @staticmethod
    def _to_api_messages(messages: Sequence[Message]) -> list[MessageParam]:
        return [cast(MessageParam, message.to_api_dict()) for message in messages]

    @staticmethod
    def _to_api_tools(tools: Sequence[ToolDefinition]) -> list[ToolParam]:
        return [cast(ToolParam, tool.to_api_dict()) for tool in tools]

    def _thinking_config(self) -> ThinkingConfigParam:
        if self.settings.thinking_enabled:
            # DeepSeek accepts the Anthropic field but ignores budget_tokens.
            return {"type": "enabled", "budget_tokens": 1024}
        return {"type": "disabled"}

    def _can_retry(self, error: APIError, retries_done: int) -> bool:
        if retries_done >= self.settings.max_retries:
            return False
        if isinstance(error, (RateLimitError, APITimeoutError, APIConnectionError)):
            return True
        return isinstance(error, APIStatusError) and (
            error.status_code == 408 or 500 <= error.status_code <= 599
        )

    def _wait_for_retry(self, error: APIError, retry_number: int) -> None:
        delay = self._retry_delay(error, retry_number)
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

    def _retry_delay(self, error: APIError, retry_number: int) -> float:
        server_delay = self._server_retry_delay(error)
        if server_delay is not None:
            return min(server_delay, self.settings.retry_max_delay)
        exponential_delay = self.settings.retry_base_delay * (2 ** (retry_number - 1))
        return min(exponential_delay, self.settings.retry_max_delay)

    def _emit_retry_activity(
        self,
        message: str,
        error: APIError,
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
    def _server_retry_delay(error: APIError) -> float | None:
        if not isinstance(error, APIStatusError):
            return None
        for header, divisor in (("retry-after-ms", 1_000), ("retry-after", 1)):
            raw_value = error.response.headers.get(header)
            if raw_value is None:
                continue
            try:
                value = float(raw_value) / divisor
            except ValueError:
                continue
            if value >= 0:
                return value
        return None

    @staticmethod
    def _retry_reason(error: APIError) -> str:
        if isinstance(error, RateLimitError):
            return "DeepSeek 限流"
        if isinstance(error, APITimeoutError):
            return "请求超时"
        if isinstance(error, APIConnectionError):
            return "连接中断"
        if isinstance(error, APIStatusError):
            return f"服务端 HTTP {error.status_code}"
        return "临时 API 错误"

    @staticmethod
    def _extract_text(content: Iterable[ContentBlock]) -> str:
        text = "".join(block.text for block in content if block.type == "text")
        if not text.strip():
            raise LLMError("模型返回了空内容，请重新尝试。")
        return text

    @staticmethod
    def _to_model_response(content: Iterable[ContentBlock]) -> ModelResponse:
        blocks = list(content)
        text = "".join(block.text for block in blocks if block.type == "text")
        tool_calls = tuple(
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in blocks
            if block.type == "tool_use"
        )
        if not text.strip() and not tool_calls:
            raise LLMError("模型返回了空内容，请重新尝试。")

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
        return ModelResponse(
            content=text,
            thinking=thinking,
            tool_calls=tool_calls,
        )

    @staticmethod
    def _friendly_error(error: APIError) -> LLMError:
        if isinstance(error, AuthenticationError):
            return LLMError("DeepSeek API Key 无效，请检查 .env 文件。")
        if isinstance(error, RateLimitError):
            return LLMError("DeepSeek 请求过于频繁，请稍后重试。")
        if isinstance(error, APITimeoutError):
            return LLMError("DeepSeek 请求超时，请检查网络后重试。")
        if isinstance(error, APIConnectionError):
            return LLMError("无法连接 DeepSeek API，请检查网络和 API 地址。")
        if isinstance(error, APIStatusError):
            return LLMError(f"DeepSeek API 请求失败（HTTP {error.status_code}）。")
        return LLMError("DeepSeek API 请求失败，请稍后重试。")
