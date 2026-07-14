"""Tests for the DeepSeek model adapter without making network requests."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

from anthropic import Anthropic

from neil_agent.config import Settings
from neil_agent.llm import LLMClient
from neil_agent.schemas import Message


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
    manager = MagicMock()
    manager.__enter__.return_value = stream
    client.messages.stream.return_value = manager
    llm = LLMClient(make_settings(), client=cast(Anthropic, client))

    chunks = list(
        llm.stream(
            [Message(role="user", content="你好")],
            system_prompt="Be helpful.",
        )
    )

    assert chunks == ["你", "好"]
