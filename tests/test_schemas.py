"""Tests for shared data schemas."""

import pytest
from pydantic import ValidationError

from neil_agent.providers.anthropic_messages import encode_message
from neil_agent.providers.base import ProviderId, ProviderTurnState
from neil_agent.schemas import Message, ThinkingContent, ToolCall, ToolResult


def test_message_remains_provider_neutral() -> None:
    message = Message(role="user", content="Hello")

    assert message.model_dump() == {
        "role": "user",
        "content": "Hello",
        "thinking": (),
        "tool_calls": (),
        "tool_results": (),
        "provider_state": None,
    }
    assert not hasattr(message, "to_api_dict")


def test_message_rejects_blank_content() -> None:
    with pytest.raises(ValidationError):
        Message(role="user", content="   ")


def test_message_rejects_provider_state_without_tool_calls() -> None:
    state = ProviderTurnState(
        provider=ProviderId.CLAUDE,
        model="claude-test-model",
        schema_version=1,
        payload={"content_blocks": ()},
    )

    with pytest.raises(ValidationError, match="requires assistant tool calls"):
        Message(role="assistant", content="done", provider_state=state)


def test_tool_schemas_are_ready_for_future_tool_loop() -> None:
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})
    result = ToolResult(tool_call_id=call.id, content="file contents")

    assert call.name == "read_file"
    assert result.is_error is False


def test_tool_messages_convert_to_anthropic_content_blocks() -> None:
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})
    assistant = Message(
        role="assistant",
        thinking=(ThinkingContent(thinking="inspect", signature="sig"),),
        tool_calls=(call,),
    )
    result = Message(
        role="user",
        tool_results=(ToolResult(tool_call_id=call.id, content="contents"),),
    )

    assistant_blocks = encode_message(assistant)["content"]
    result_blocks = encode_message(result)["content"]
    assert assistant_blocks[0]["type"] == "thinking"
    assert assistant_blocks[1]["type"] == "tool_use"
    assert result_blocks[0]["tool_use_id"] == "call-1"
