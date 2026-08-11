"""Native OpenAI adapter for the Responses API."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from time import sleep
from typing import Any

from openai import APIError, OpenAI

from ..config import Settings, get_settings
from ..schemas import ActivityEvent, Message, ModelResponse, TokenUsage, ToolDefinition
from .base import (
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderId,
    WireProtocol,
)
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
from .openai_responses import (
    encode_messages,
    encode_tools,
    normalize_openai_error,
    normalize_stream_error,
    parse_response,
)
from .retry import RetryPolicy

RetryHandler = Callable[[ActivityEvent], None]
Sleeper = Callable[[float], None]
OpenAIClientFactory = Callable[..., OpenAI]

OPENAI_DESCRIPTOR = ProviderDescriptor(
    provider=ProviderId.OPENAI,
    display_name="OpenAI",
    wire_protocol=WireProtocol.OPENAI_RESPONSES,
    capabilities=ProviderCapabilities(
        streaming=True,
        tool_calling=True,
        parallel_tool_calls=True,
        reasoning_state=True,
        structured_output=False,
        usage_reporting=True,
        prompt_caching=True,
    ),
)

_PASSIVE_STREAM_EVENTS = frozenset(
    {
        "response.queued",
        "response.in_progress",
        "response.output_item.added",
        "response.output_item.done",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.done",
        "response.refusal.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
        "response.output_text.annotation.added",
    }
)


class OpenAIProvider:
    """Auditable OpenAI Responses request, stream, state, and retry runtime."""

    provider_id = ProviderId.OPENAI
    provider_descriptor = OPENAI_DESCRIPTOR

    def __init__(
        self,
        settings: Settings | None = None,
        client: OpenAI | None = None,
        retry_handler: RetryHandler | None = None,
        sleeper: Sleeper = sleep,
        *,
        client_factory: OpenAIClientFactory | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if self.settings.llm_provider is not ProviderId.OPENAI:
            raise ProviderNotImplementedError(
                f"Provider '{self.settings.llm_provider.value}' cannot be started "
                "through the OpenAI adapter.",
                provider=self.settings.llm_provider,
            )
        api_key = self.settings.selected_api_key
        if api_key is None:
            raise ProviderAuthenticationError(
                "未配置 OpenAI API Key。",
                provider=ProviderId.OPENAI,
            )

        factory = client_factory or OpenAI
        if client is None:
            client = factory(
                api_key=api_key.get_secret_value(),
                base_url=self._base_url(),
                timeout=self.settings.request_timeout,
                max_retries=0,
            )
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
        """Return safe metadata and this adapter's capability snapshot."""

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
        """Return one complete text response from native Responses."""

        self._ensure_messages(messages)
        self._last_usage = None
        request = self._request_kwargs(messages, system_prompt=system_prompt)
        retries_done = 0
        while True:
            try:
                response = self._client.responses.create(**request)
                model_response = parse_response(
                    response,
                    model=self.settings.selected_model,
                )
                if model_response.tool_calls:
                    raise ProviderProtocolError(
                        "非流式完成返回了 function call。",
                        provider=ProviderId.OPENAI,
                    )
                self._last_usage = model_response.usage
                return model_response.content
            except Exception as sdk_error:
                error = self._normalize_runtime_error(sdk_error)
                if not self._retry_policy.can_retry(error, retries_done):
                    if error is sdk_error:
                        raise error
                    raise error from sdk_error
                retries_done += 1
                self._wait_for_retry(error, retries_done)

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        """Yield visible deltas and one validated terminal ModelResponse."""

        self._ensure_messages(messages)
        self._last_usage = None
        request = self._request_kwargs(
            messages,
            system_prompt=system_prompt,
            tools=tools,
        )
        retries_done = 0
        while True:
            output_started = False
            created = False
            terminal_response: object | None = None
            terminal_type: str | None = None
            previous_sequence = -1
            visible_text: list[str] = []
            argument_deltas: dict[str, list[str]] = {}
            argument_done: dict[str, str] = {}
            try:
                raw_stream = self._client.responses.create(**request, stream=True)
                with raw_stream as stream:
                    for event in stream:
                        event_type = self._event_string(event, "type")
                        previous_sequence = self._validate_sequence(
                            event,
                            previous_sequence,
                        )
                        if event_type == "error":
                            raise normalize_stream_error(
                                self._event_field(event, "code")
                            )
                        if not created:
                            if event_type != "response.created":
                                raise ProviderProtocolError(
                                    "OpenAI 流在 response.created 之前返回了内容。",
                                    provider=ProviderId.OPENAI,
                                )
                            created = True
                            continue
                        if terminal_response is not None:
                            raise ProviderProtocolError(
                                "OpenAI 流在终态事件后继续返回内容。",
                                provider=ProviderId.OPENAI,
                            )
                        if event_type in {
                            "response.output_text.delta",
                            "response.refusal.delta",
                        }:
                            delta = self._event_string(event, "delta", allow_empty=True)
                            if delta:
                                output_started = True
                                visible_text.append(delta)
                                yield delta
                        elif event_type == "response.function_call_arguments.delta":
                            output_started = True
                            item_id = self._event_string(event, "item_id")
                            delta = self._event_string(event, "delta", allow_empty=True)
                            argument_deltas.setdefault(item_id, []).append(delta)
                        elif event_type == "response.function_call_arguments.done":
                            item_id = self._event_string(event, "item_id")
                            arguments = self._event_string(event, "arguments")
                            if item_id in argument_done:
                                raise ProviderProtocolError(
                                    "OpenAI 流重复结束同一个 function call 参数。",
                                    provider=ProviderId.OPENAI,
                                )
                            argument_done[item_id] = arguments
                        elif event_type in {
                            "response.completed",
                            "response.incomplete",
                            "response.failed",
                        }:
                            terminal_response = self._event_field(event, "response")
                            if terminal_response is None:
                                raise ProviderProtocolError(
                                    "OpenAI 终态事件缺少 response。",
                                    provider=ProviderId.OPENAI,
                                )
                            terminal_type = event_type
                        elif event_type not in _PASSIVE_STREAM_EVENTS:
                            raise ProviderProtocolError(
                                f"OpenAI 流返回了未支持的事件：{event_type}。",
                                provider=ProviderId.OPENAI,
                            )
                if terminal_response is None or terminal_type is None:
                    raise ProviderProtocolError(
                        "OpenAI 流在没有终态事件时结束。",
                        provider=ProviderId.OPENAI,
                    )
                self._validate_terminal_type(terminal_response, terminal_type)
                if terminal_type == "response.failed":
                    # Preserve the provider error category even when a failed
                    # response has no output collection.
                    parse_response(
                        terminal_response,
                        model=self.settings.selected_model,
                    )
                    raise ProviderProtocolError(
                        "OpenAI failed 终态被错误解析为成功。",
                        provider=ProviderId.OPENAI,
                    )
                self._validate_argument_deltas(
                    terminal_response,
                    argument_deltas,
                    argument_done,
                )
                model_response = parse_response(
                    terminal_response,
                    model=self.settings.selected_model,
                )
                if "".join(visible_text) != model_response.content:
                    raise ProviderProtocolError(
                        "OpenAI 文本 delta 与终态响应不一致。",
                        provider=ProviderId.OPENAI,
                    )
                self._last_usage = model_response.usage
                yield model_response
                return
            except Exception as sdk_error:
                error = self._normalize_runtime_error(sdk_error)
                if not self._retry_policy.can_retry(
                    error,
                    retries_done,
                    output_started=output_started,
                ):
                    if error is sdk_error:
                        raise error
                    raise error from sdk_error
                retries_done += 1
                self._wait_for_retry(error, retries_done)

    def _request_kwargs(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.settings.selected_model,
            "max_output_tokens": self.settings.max_tokens,
            "instructions": system_prompt,
            "input": encode_messages(
                messages,
                model=self.settings.selected_model,
            ),
            "store": False,
        }
        if tools is not None:
            request["tools"] = encode_tools(tools)
        if self.settings.thinking_enabled:
            request["reasoning"] = {
                "effort": self.settings.openai_reasoning_effort,
            }
            request["include"] = ["reasoning.encrypted_content"]
        return request

    def _base_url(self) -> str:
        selected = self.settings.selected_base_url
        if selected is None:
            raise ProviderProtocolError(
                "未配置 OpenAI API 地址。",
                provider=ProviderId.OPENAI,
            )
        return str(selected).rstrip("/")

    @staticmethod
    def _ensure_messages(messages: Sequence[Message]) -> None:
        if not messages:
            raise ValueError("at least one message is required")

    @staticmethod
    def _event_field(event: object, field: str) -> object:
        if isinstance(event, Mapping):
            return event.get(field)
        return getattr(event, field, None)

    @classmethod
    def _event_string(
        cls,
        event: object,
        field: str,
        *,
        allow_empty: bool = False,
    ) -> str:
        value = cls._event_field(event, field)
        if not isinstance(value, str) or (not allow_empty and not value):
            raise ProviderProtocolError(
                f"OpenAI 流事件缺少有效的 {field} 字段。",
                provider=ProviderId.OPENAI,
            )
        return value

    @classmethod
    def _validate_sequence(cls, event: object, previous: int) -> int:
        value = cls._event_field(event, "sequence_number")
        if isinstance(value, bool) or not isinstance(value, int) or value <= previous:
            raise ProviderProtocolError(
                "OpenAI 流事件 sequence_number 非严格递增。",
                provider=ProviderId.OPENAI,
            )
        return value

    @classmethod
    def _validate_terminal_type(cls, response: object, event_type: str) -> None:
        status = cls._event_field(response, "status")
        expected = {
            "response.completed": "completed",
            "response.incomplete": "incomplete",
            "response.failed": "failed",
        }[event_type]
        if status != expected:
            raise ProviderProtocolError(
                "OpenAI 终态事件与 response status 不一致。",
                provider=ProviderId.OPENAI,
            )

    @classmethod
    def _validate_argument_deltas(
        cls,
        response: object,
        deltas: Mapping[str, Sequence[str]],
        done: Mapping[str, str],
    ) -> None:
        output = cls._event_field(response, "output")
        if not isinstance(output, Sequence) or isinstance(output, (str, bytes)):
            raise ProviderProtocolError(
                "OpenAI 终态响应缺少有效 output。",
                provider=ProviderId.OPENAI,
            )
        terminal: dict[str, str] = {}
        for item in output:
            if cls._event_field(item, "type") != "function_call":
                continue
            item_id = cls._event_string(item, "id")
            arguments = cls._event_string(item, "arguments")
            if item_id in terminal:
                raise ProviderProtocolError(
                    "OpenAI 终态响应包含重复 function item ID。",
                    provider=ProviderId.OPENAI,
                )
            terminal[item_id] = arguments
        if set(done) != set(terminal) or not set(deltas).issubset(done):
            raise ProviderProtocolError(
                "OpenAI function call 参数流缺少完成事件。",
                provider=ProviderId.OPENAI,
            )
        for item_id, arguments in done.items():
            if arguments != terminal[item_id]:
                raise ProviderProtocolError(
                    "OpenAI function call 完成值与终态响应不一致。",
                    provider=ProviderId.OPENAI,
                )
        for item_id, parts in deltas.items():
            if "".join(parts) != done[item_id]:
                raise ProviderProtocolError(
                    "OpenAI function call 参数 delta 与完成值不一致。",
                    provider=ProviderId.OPENAI,
                )

    @staticmethod
    def _normalize_runtime_error(error: Exception) -> ProviderError:
        if isinstance(error, ProviderError):
            return error
        if isinstance(error, APIError):
            return normalize_openai_error(error)
        return ProviderProtocolError(
            "OpenAI SDK 返回了无法解析的响应。",
            provider=ProviderId.OPENAI,
        )

    def _wait_for_retry(self, error: ProviderError, retry_number: int) -> None:
        delay = self._retry_policy.delay(error, retry_number)
        self._emit_retry_activity(error, retry_number, delay)
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
        error: ProviderError,
        retry_number: int,
        delay: float,
    ) -> None:
        if self._retry_handler is None:
            return
        self._retry_handler(
            ActivityEvent(
                status="running",
                message="模型请求暂时失败，等待重试",
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
            return "OpenAI 限流"
        if isinstance(error, ProviderTimeoutError):
            return "请求超时"
        if isinstance(error, ProviderConnectionError):
            return "连接中断"
        if isinstance(error, ProviderInternalError) and error.status_code is not None:
            return f"服务端 HTTP {error.status_code}"
        return "Provider 临时错误"
