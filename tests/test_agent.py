"""Tests for conversation history and streaming behavior."""

from collections.abc import Iterator, Sequence

import pytest

from neil_agent.agent import Agent
from neil_agent.llm import LLMError
from neil_agent.schemas import Message, ModelResponse, ToolCall, ToolDefinition
from neil_agent.tools.registry import ToolRegistry


class FakeChatModel:
    def __init__(self, response: str = "assistant reply") -> None:
        self.response = response
        self.requests: list[list[Message]] = []
        self.system_prompts: list[str] = []
        self.tool_definitions: list[list[ToolDefinition]] = []
        self.stream_responses: list[list[str | ModelResponse]] = []
        self.fail_stream = False

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        self.requests.append(list(messages))
        self.system_prompts.append(system_prompt)
        return self.response

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self.requests.append(list(messages))
        self.system_prompts.append(system_prompt)
        self.tool_definitions.append(list(tools))
        if self.fail_stream:
            raise LLMError("request failed")
        events = (
            self.stream_responses.pop(0)
            if self.stream_responses
            else [
                "assistant ",
                "reply",
                ModelResponse(content="assistant reply"),
            ]
        )
        yield from events


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


def test_agent_passes_custom_system_prompt_to_model() -> None:
    model = FakeChatModel()
    agent = Agent(model, system_prompt="You are a Python tutor.")

    agent.chat("hello")

    assert model.system_prompts == ["You are a Python tutor."]


def test_agent_executes_tool_and_returns_result_to_model() -> None:
    model = FakeChatModel()
    model.stream_responses = [
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "README.md"},
                    ),
                )
            )
        ],
        ["done", ModelResponse(content="done")],
    ]
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read_file",
            description="Read a file.",
            input_schema={"type": "object"},
        ),
        lambda path: f"contents of {path}",
    )
    agent = Agent(model, registry=registry)

    chunks = list(agent.stream_chat("read the README"))

    assert chunks == ["done"]
    assert model.tool_definitions[0][0].name == "read_file"
    tool_result_message = model.requests[1][-1]
    assert tool_result_message.tool_results[0].content == "contents of README.md"
    assert len(agent.messages) == 4


def test_agent_stops_when_tool_round_limit_is_exceeded() -> None:
    call = ToolCall(id="call-1", name="repeat", arguments={})
    model = FakeChatModel()
    model.stream_responses = [
        [ModelResponse(tool_calls=(call,))],
        [ModelResponse(tool_calls=(call.model_copy(update={"id": "call-2"}),))],
    ]
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="repeat",
            description="Ask to repeat.",
            input_schema={"type": "object"},
        ),
        lambda: "repeat",
    )
    agent = Agent(model, registry=registry, max_tool_rounds=1)

    with pytest.raises(LLMError, match="超过 1 轮"):
        list(agent.stream_chat("keep going"))

    assert agent.messages == ()
