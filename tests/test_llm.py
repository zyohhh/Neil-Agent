"""Tests for the DeepSeek model adapter without making network requests."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import (
    APIConnectionError,
    APIStatusError,
    Anthropic,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

from neil_agent import llm as llm_module
from neil_agent.config import Settings
from neil_agent.errors import LLMError
from neil_agent.llm import LLMClient
from neil_agent.schemas import ActivityEvent, Message, ModelResponse, ToolDefinition


def make_settings(*, thinking_enabled: bool = False) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        deepseek_model="deepseek-v4-flash",
        thinking_enabled=thinking_enabled,
    )


def test_complete_extracts_text_content() -> None:
    client = MagicMock(spec=Anthropic)
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hello")]
    )
    llm = LLMClient(make_settings(), client=cast(Anthropic, client))

    response = llm.complete(
        [Message(role="user", content="hi")],
        system_prompt="Be helpful.",
    )

    assert response == "hello"
    request = client.messages.create.call_args.kwargs
    assert request["model"] == "deepseek-v4-flash"
    assert request["thinking"] == {"type": "disabled"}


def test_default_sdk_client_disables_hidden_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=Anthropic)
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(llm_module, "Anthropic", factory)

    LLMClient(make_settings())

    assert factory.call_args.kwargs["max_retries"] == 0


def test_complete_enables_thinking_from_settings() -> None:
    client = MagicMock(spec=Anthropic)
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="reasoned answer")]
    )
    llm = LLMClient(
        make_settings(thinking_enabled=True),
        client=cast(Anthropic, client),
    )

    llm.complete(
        [Message(role="user", content="solve this")],
        system_prompt="Think carefully.",
    )

    request = client.messages.create.call_args.kwargs
    assert request["thinking"] == {"type": "enabled", "budget_tokens": 1024}


def test_stream_yields_text_fragments() -> None:
    client = MagicMock(spec=Anthropic)
    stream = MagicMock()
    stream.text_stream = iter(["你", "好"])
    stream.get_final_message.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="你好")]
    )
    manager = MagicMock()
    manager.__enter__.return_value = stream
    client.messages.stream.return_value = manager
    llm = LLMClient(make_settings(), client=cast(Anthropic, client))

    events = list(
        llm.stream(
            [Message(role="user", content="你好")],
            system_prompt="Be helpful.",
        )
    )

    assert events[:2] == ["你", "好"]
    assert events[2] == ModelResponse(content="你好")


def test_stream_returns_tool_call_and_replayable_thinking() -> None:
    client = MagicMock(spec=Anthropic)
    stream = MagicMock()
    stream.text_stream = iter([])
    stream.get_final_message.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="inspect", signature="sig"),
            SimpleNamespace(
                type="tool_use",
                id="call-1",
                name="read_file",
                input={"path": "README.md"},
            ),
        ]
    )
    manager = MagicMock()
    manager.__enter__.return_value = stream
    client.messages.stream.return_value = manager
    llm = LLMClient(
        make_settings(thinking_enabled=True), client=cast(Anthropic, client)
    )
    definition = ToolDefinition(
        name="read_file",
        description="Read a file.",
        input_schema={"type": "object"},
    )

    events = list(
        llm.stream(
            [Message(role="user", content="Read README")],
            system_prompt="Use tools.",
            tools=[definition],
        )
    )

    response = cast(ModelResponse, events[-1])
    assert response.tool_calls[0].name == "read_file"
    assert response.thinking[0].signature == "sig"
    assert client.messages.stream.call_args.kwargs["tools"][0]["name"] == "read_file"


def test_complete_retries_connection_error_with_visible_backoff() -> None:
    client = MagicMock(spec=Anthropic)
    request = httpx.Request("POST", "https://api.deepseek.com/messages")
    client.messages.create.side_effect = [
        APIConnectionError(request=request),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="recovered")]),
    ]
    settings = make_settings().model_copy(
        update={"max_retries": 2, "retry_base_delay": 0.5}
    )
    activities: list[ActivityEvent] = []
    delays: list[float] = []
    llm = LLMClient(
        settings,
        client=cast(Anthropic, client),
        retry_handler=activities.append,
        sleeper=delays.append,
    )

    response = llm.complete(
        [Message(role="user", content="hello")],
        system_prompt="Be helpful.",
    )

    assert response == "recovered"
    assert client.messages.create.call_count == 2
    assert delays == [0.5]
    assert [activity.message for activity in activities] == [
        "模型请求暂时失败，等待重试",
        "重试模型请求",
    ]
    assert activities[0].details == (
        "原因：连接中断",
        "重试：1/2",
        "等待：0.5 秒",
    )


def test_retry_after_header_is_bounded_by_configured_maximum() -> None:
    client = MagicMock(spec=Anthropic)
    request = httpx.Request("POST", "https://api.deepseek.com/messages")
    response = httpx.Response(429, headers={"retry-after": "30"}, request=request)
    client.messages.create.side_effect = [
        RateLimitError("limited", response=response, body=None),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")]),
    ]
    settings = make_settings().model_copy(
        update={"max_retries": 1, "retry_max_delay": 2.0}
    )
    delays: list[float] = []
    llm = LLMClient(
        settings,
        client=cast(Anthropic, client),
        sleeper=delays.append,
    )

    assert (
        llm.complete(
            [Message(role="user", content="hello")],
            system_prompt="Be helpful.",
        )
        == "ok"
    )
    assert delays == [2.0]


def test_authentication_error_is_not_retried() -> None:
    client = MagicMock(spec=Anthropic)
    request = httpx.Request("POST", "https://api.deepseek.com/messages")
    response = httpx.Response(401, request=request)
    client.messages.create.side_effect = AuthenticationError(
        "invalid key",
        response=response,
        body=None,
    )
    delays: list[float] = []
    llm = LLMClient(
        make_settings(),
        client=cast(Anthropic, client),
        sleeper=delays.append,
    )

    with pytest.raises(LLMError, match="API Key 无效"):
        llm.complete(
            [Message(role="user", content="hello")],
            system_prompt="Be helpful.",
        )

    assert client.messages.create.call_count == 1
    assert delays == []


def test_bad_request_is_not_retried() -> None:
    client = MagicMock(spec=Anthropic)
    request = httpx.Request("POST", "https://api.deepseek.com/messages")
    response = httpx.Response(400, request=request)
    client.messages.create.side_effect = BadRequestError(
        "invalid request",
        response=response,
        body=None,
    )
    delays: list[float] = []
    llm = LLMClient(
        make_settings(),
        client=cast(Anthropic, client),
        sleeper=delays.append,
    )

    with pytest.raises(LLMError, match="HTTP 400"):
        llm.complete(
            [Message(role="user", content="hello")],
            system_prompt="Be helpful.",
        )

    assert client.messages.create.call_count == 1
    assert delays == []


def test_transient_error_stops_after_configured_retry_limit() -> None:
    client = MagicMock(spec=Anthropic)
    request = httpx.Request("POST", "https://api.deepseek.com/messages")
    client.messages.create.side_effect = APIConnectionError(request=request)
    settings = make_settings().model_copy(
        update={"max_retries": 2, "retry_base_delay": 0.0}
    )
    delays: list[float] = []
    llm = LLMClient(
        settings,
        client=cast(Anthropic, client),
        sleeper=delays.append,
    )

    with pytest.raises(LLMError, match="无法连接"):
        llm.complete(
            [Message(role="user", content="hello")],
            system_prompt="Be helpful.",
        )

    assert client.messages.create.call_count == 3
    assert delays == [0.0, 0.0]


def test_stream_retries_server_error_before_text_is_emitted() -> None:
    client = MagicMock(spec=Anthropic)
    request = httpx.Request("POST", "https://api.deepseek.com/messages")
    response = httpx.Response(503, request=request)
    stream = MagicMock()
    stream.text_stream = iter(["done"])
    stream.get_final_message.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="done")]
    )
    manager = MagicMock()
    manager.__enter__.return_value = stream
    client.messages.stream.side_effect = [
        APIStatusError("unavailable", response=response, body=None),
        manager,
    ]
    delays: list[float] = []
    settings = make_settings().model_copy(update={"retry_base_delay": 0.0})
    llm = LLMClient(
        settings,
        client=cast(Anthropic, client),
        sleeper=delays.append,
    )

    events = list(
        llm.stream(
            [Message(role="user", content="hello")],
            system_prompt="Be helpful.",
        )
    )

    assert events == ["done", ModelResponse(content="done")]
    assert client.messages.stream.call_count == 2
    assert delays == [0.0]


def test_stream_does_not_retry_after_text_was_emitted() -> None:
    client = MagicMock(spec=Anthropic)
    request = httpx.Request("POST", "https://api.deepseek.com/messages")

    def partial_stream() -> object:
        yield "partial"
        raise APIConnectionError(request=request)

    stream = MagicMock()
    stream.text_stream = partial_stream()
    manager = MagicMock()
    manager.__enter__.return_value = stream
    manager.__exit__.return_value = False
    client.messages.stream.return_value = manager
    delays: list[float] = []
    llm = LLMClient(
        make_settings(),
        client=cast(Anthropic, client),
        sleeper=delays.append,
    )

    events = llm.stream(
        [Message(role="user", content="hello")],
        system_prompt="Be helpful.",
    )

    assert next(events) == "partial"
    with pytest.raises(LLMError, match="无法连接"):
        next(events)
    assert client.messages.stream.call_count == 1
    assert delays == []
