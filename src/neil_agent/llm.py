"""DeepSeek model integration through its Anthropic-compatible API."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
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
    Message,
    ModelResponse,
    ThinkingContent,
    ToolCall,
    ToolDefinition,
)


class LLMClient:
    """Small wrapper around the Anthropic SDK configured for DeepSeek."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: Anthropic | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client or Anthropic(
            api_key=self.settings.deepseek_api_key.get_secret_value(),
            base_url=str(self.settings.deepseek_base_url).rstrip("/"),
            timeout=self.settings.request_timeout,
        )

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        """Return one complete model response without streaming."""

        self._ensure_messages(messages)
        try:
            response = self._client.messages.create(
                model=self.settings.deepseek_model,
                max_tokens=self.settings.max_tokens,
                system=system_prompt,
                messages=self._to_api_messages(messages),
                thinking=self._thinking_config(),
            )
        except APIError as error:
            raise self._friendly_error(error) from error

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
                        yield text
                final_message = stream.get_final_message()
        except APIError as error:
            raise self._friendly_error(error) from error

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
