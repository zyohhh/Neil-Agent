"""Tests for the DeepSeek model adapter without making network requests."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

from anthropic import Anthropic

from neil_agent.config import Settings
from neil_agent.llm import LLMClient
from neil_agent.schemas import Message, ModelResponse, ToolDefinition


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
