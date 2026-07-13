"""Tests for conversation history and streaming behavior."""

from collections.abc import Iterator, Sequence

import pytest

from neil_agent.agent import Agent
from neil_agent.llm import LLMError
from neil_agent.schemas import Message


class FakeChatModel:
    def __init__(self, response: str = "assistant reply") -> None:
        self.response = response
        self.requests: list[list[Message]] = []
        self.fail_stream = False

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        self.requests.append(list(messages))
        return self.response

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> Iterator[str]:
        self.requests.append(list(messages))
        if self.fail_stream:
            raise LLMError("request failed")
        yield "assistant "
        yield "reply"


def test_stream_chat_saves_complete_round() -> None:
    model = FakeChatModel()
    agent = Agent(model)

    chunks = list(agent.stream_chat("hello"))

    assert chunks == ["assistant ", "reply"]
    assert [(message.role, message.content) for message in agent.messages] == [
        ("user", "hello"),
        ("assistant", "assistant reply"),
    ]


def test_failed_stream_does_not_change_history() -> None:
    model = FakeChatModel()
    model.fail_stream = True
    agent = Agent(model)

    with pytest.raises(LLMError, match="request failed"):
        list(agent.stream_chat("hello"))

    assert agent.messages == ()


def test_max_rounds_keeps_only_recent_context() -> None:
    model = FakeChatModel()
    agent = Agent(model, max_rounds=2)

    agent.chat("first")
    agent.chat("second")
    agent.chat("third")

    assert [message.content for message in model.requests[-1]] == [
        "second",
        "assistant reply",
        "third",
    ]
    assert [message.content for message in agent.messages] == [
        "second",
        "assistant reply",
        "third",
        "assistant reply",
    ]


def test_clear_removes_conversation_history() -> None:
    agent = Agent(FakeChatModel())
    agent.chat("hello")

    agent.clear()

    assert agent.messages == ()
