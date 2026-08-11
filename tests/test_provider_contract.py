"""Golden characterization tests for the first provider contract version."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

from anthropic import Anthropic

from neil_agent.agent import ChatModel, UsageReportingChatModel
from neil_agent.config import Settings
from neil_agent.llm import LLMClient
from neil_agent.providers.claude import ClaudeProvider
from neil_agent.providers.base import ProviderId
from neil_agent.schemas import Message, ModelResponse, TokenUsage, ToolDefinition

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "providers"
    / "deepseek_anthropic_messages_v1.json"
)
CLAUDE_FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "providers"
    / "claude_anthropic_messages_v1.json"
)


def assert_complete_contract(
    model: ChatModel,
    messages: Sequence[Message],
    *,
    system_prompt: str,
    expected_text: str,
    expected_usage: TokenUsage | None,
) -> None:
    """Assert the stable non-streaming ChatModel contract."""

    result = model.complete(messages, system_prompt=system_prompt)

    assert isinstance(result, str)
    assert result == expected_text
    assert result.strip()
    if isinstance(model, UsageReportingChatModel):
        assert model.last_usage == expected_usage


def assert_stream_contract(
    model: ChatModel,
    messages: Sequence[Message],
    *,
    system_prompt: str,
    tools: Sequence[ToolDefinition],
    expected_deltas: Sequence[str],
    expected_response: ModelResponse,
) -> None:
    """Assert event ordering and terminal response invariants for streaming."""

    events = tuple(model.stream(messages, system_prompt=system_prompt, tools=tools))

    assert events
    assert all(isinstance(event, str) for event in events[:-1])
    assert all(cast(str, event) for event in events[:-1])
    assert tuple(cast(str, event) for event in events[:-1]) == tuple(expected_deltas)
    assert isinstance(events[-1], ModelResponse)

    response = cast(ModelResponse, events[-1])
    assert response == expected_response
    assert response.content.strip() or response.tool_calls
    tool_call_ids = tuple(call.id for call in response.tool_calls)
    assert len(tool_call_ids) == len(set(tool_call_ids))
    if response.thinking:
        assert response.tool_calls
    if isinstance(model, UsageReportingChatModel):
        assert model.last_usage == response.usage


def _fixture(path: Path = FIXTURE_PATH) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(path.read_text(encoding="utf-8")),
    )


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _items(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    assert all(isinstance(item, dict) for item in value)
    return cast(list[dict[str, object]], value)


def _messages(value: object) -> list[Message]:
    return [Message.model_validate(item) for item in _items(value)]


def _tools(value: object) -> list[ToolDefinition]:
    return [ToolDefinition.model_validate(item) for item in _items(value)]


def _sdk_message(value: object) -> SimpleNamespace:
    payload = _mapping(value)
    usage = payload.get("usage")
    return SimpleNamespace(
        content=[SimpleNamespace(**block) for block in _items(payload["content"])],
        usage=(SimpleNamespace(**_mapping(usage)) if usage is not None else None),
        stop_reason=payload.get("stop_reason"),
    )


def _settings(fixture: dict[str, object], *, thinking_enabled: bool) -> Settings:
    settings = _mapping(fixture["settings"])
    values: dict[str, object] = {
        "llm_provider": fixture["provider"],
        "llm_model": cast(str, settings["model"]),
        "max_tokens": cast(int, settings["max_tokens"]),
        "thinking_enabled": thinking_enabled,
    }
    if fixture["provider"] == ProviderId.DEEPSEEK:
        values.update(
            {
                "deepseek_api_key": "contract-test-key",
                "deepseek_model": cast(str, settings["model"]),
            }
        )
    else:
        values["anthropic_api_key"] = "contract-test-key"
    return Settings.model_validate(values)


def _case(fixture: dict[str, object], name: str) -> dict[str, object]:
    return _mapping(_mapping(fixture["cases"])[name])


def test_deepseek_fixture_is_versioned_synthetic_and_secret_free() -> None:
    raw_fixture = FIXTURE_PATH.read_text(encoding="utf-8")
    fixture = _fixture()

    assert fixture["fixture_version"] == 1
    assert fixture["provider"] == "deepseek"
    assert fixture["wire_protocol"] == "anthropic-messages"
    assert _mapping(fixture["source"]) == {
        "kind": "synthetic-characterization",
        "live_capture": False,
        "sanitized": True,
        "sdk": "anthropic",
    }
    assert "api_key" not in raw_fixture.lower()
    assert "contract-test-key" not in raw_fixture


def test_deepseek_complete_matches_v1_golden_contract() -> None:
    fixture = _fixture()
    case = _case(fixture, "complete_text")
    input_payload = _mapping(case["input"])
    wire_response = _mapping(case["wire_response"])
    contract = _mapping(case["contract"])
    client = MagicMock(spec=Anthropic)
    client.messages.create.return_value = _sdk_message(wire_response)
    model = LLMClient(
        _settings(fixture, thinking_enabled=False),
        client=cast(Anthropic, client),
    )

    assert_complete_contract(
        model,
        _messages(input_payload["messages"]),
        system_prompt=cast(str, input_payload["system_prompt"]),
        expected_text=cast(str, contract["result"]),
        expected_usage=TokenUsage.model_validate(contract["usage"]),
    )

    assert client.messages.create.call_args.kwargs == case["wire_request"]


def test_deepseek_text_stream_matches_v1_golden_contract() -> None:
    fixture = _fixture()
    case = _case(fixture, "stream_text")
    input_payload = _mapping(case["input"])
    wire_stream = _mapping(case["wire_stream"])
    contract = _mapping(case["contract"])
    client = MagicMock(spec=Anthropic)
    stream = MagicMock()
    stream.text_stream = iter(cast(list[str], wire_stream["text_deltas"]))
    stream.get_final_message.return_value = _sdk_message(wire_stream["final_message"])
    manager = MagicMock()
    manager.__enter__.return_value = stream
    client.messages.stream.return_value = manager
    model = LLMClient(
        _settings(fixture, thinking_enabled=False),
        client=cast(Anthropic, client),
    )

    assert_stream_contract(
        model,
        _messages(input_payload["messages"]),
        system_prompt=cast(str, input_payload["system_prompt"]),
        tools=_tools(input_payload["tools"]),
        expected_deltas=cast(list[str], contract["text_deltas"]),
        expected_response=ModelResponse.model_validate(contract["response"]),
    )

    assert client.messages.stream.call_args.kwargs == case["wire_request"]


def test_deepseek_tool_stream_matches_v1_golden_contract() -> None:
    fixture = _fixture()
    case = _case(fixture, "stream_tool_call")
    input_payload = _mapping(case["input"])
    wire_stream = _mapping(case["wire_stream"])
    contract = _mapping(case["contract"])
    client = MagicMock(spec=Anthropic)
    stream = MagicMock()
    stream.text_stream = iter(cast(list[str], wire_stream["text_deltas"]))
    stream.get_final_message.return_value = _sdk_message(wire_stream["final_message"])
    manager = MagicMock()
    manager.__enter__.return_value = stream
    client.messages.stream.return_value = manager
    model = LLMClient(
        _settings(fixture, thinking_enabled=True),
        client=cast(Anthropic, client),
    )

    assert_stream_contract(
        model,
        _messages(input_payload["messages"]),
        system_prompt=cast(str, input_payload["system_prompt"]),
        tools=_tools(input_payload["tools"]),
        expected_deltas=cast(list[str], contract["text_deltas"]),
        expected_response=ModelResponse.model_validate(contract["response"]),
    )

    assert client.messages.stream.call_args.kwargs == case["wire_request"]


def test_claude_fixture_is_versioned_synthetic_and_secret_free() -> None:
    raw_fixture = CLAUDE_FIXTURE_PATH.read_text(encoding="utf-8")
    fixture = _fixture(CLAUDE_FIXTURE_PATH)

    assert fixture["fixture_version"] == 1
    assert fixture["provider"] == "claude"
    assert fixture["wire_protocol"] == "anthropic-messages"
    assert _mapping(fixture["source"]) == {
        "kind": "synthetic-characterization",
        "live_capture": False,
        "sanitized": True,
        "sdk": "anthropic",
    }
    assert "api_key" not in raw_fixture.lower()
    assert "contract-test-key" not in raw_fixture


def test_claude_complete_matches_v1_golden_contract() -> None:
    fixture = _fixture(CLAUDE_FIXTURE_PATH)
    case = _case(fixture, "complete_text")
    input_payload = _mapping(case["input"])
    wire_response = _mapping(case["wire_response"])
    contract = _mapping(case["contract"])
    client = MagicMock(spec=Anthropic)
    client.messages.create.return_value = _sdk_message(wire_response)
    model = ClaudeProvider(
        _settings(fixture, thinking_enabled=False),
        client=cast(Anthropic, client),
    )

    assert_complete_contract(
        model,
        _messages(input_payload["messages"]),
        system_prompt=cast(str, input_payload["system_prompt"]),
        expected_text=cast(str, contract["result"]),
        expected_usage=TokenUsage.model_validate(contract["usage"]),
    )

    assert client.messages.create.call_args.kwargs == case["wire_request"]


def test_claude_text_stream_matches_v1_golden_contract() -> None:
    fixture = _fixture(CLAUDE_FIXTURE_PATH)
    case = _case(fixture, "stream_text")
    input_payload = _mapping(case["input"])
    wire_stream = _mapping(case["wire_stream"])
    contract = _mapping(case["contract"])
    client = MagicMock(spec=Anthropic)
    stream = MagicMock()
    stream.text_stream = iter(cast(list[str], wire_stream["text_deltas"]))
    stream.get_final_message.return_value = _sdk_message(wire_stream["final_message"])
    manager = MagicMock()
    manager.__enter__.return_value = stream
    client.messages.stream.return_value = manager
    model = ClaudeProvider(
        _settings(fixture, thinking_enabled=False),
        client=cast(Anthropic, client),
    )

    assert_stream_contract(
        model,
        _messages(input_payload["messages"]),
        system_prompt=cast(str, input_payload["system_prompt"]),
        tools=_tools(input_payload["tools"]),
        expected_deltas=cast(list[str], contract["text_deltas"]),
        expected_response=ModelResponse.model_validate(contract["response"]),
    )

    assert client.messages.stream.call_args.kwargs == case["wire_request"]


def test_claude_tool_stream_matches_v1_golden_contract() -> None:
    fixture = _fixture(CLAUDE_FIXTURE_PATH)
    case = _case(fixture, "stream_tool_call")
    input_payload = _mapping(case["input"])
    wire_stream = _mapping(case["wire_stream"])
    contract = _mapping(case["contract"])
    client = MagicMock(spec=Anthropic)
    stream = MagicMock()
    stream.text_stream = iter(cast(list[str], wire_stream["text_deltas"]))
    stream.get_final_message.return_value = _sdk_message(wire_stream["final_message"])
    manager = MagicMock()
    manager.__enter__.return_value = stream
    client.messages.stream.return_value = manager
    model = ClaudeProvider(
        _settings(fixture, thinking_enabled=True),
        client=cast(Anthropic, client),
    )

    assert_stream_contract(
        model,
        _messages(input_payload["messages"]),
        system_prompt=cast(str, input_payload["system_prompt"]),
        tools=_tools(input_payload["tools"]),
        expected_deltas=cast(list[str], contract["text_deltas"]),
        expected_response=ModelResponse.model_validate(contract["response"]),
    )

    assert client.messages.stream.call_args.kwargs == case["wire_request"]
