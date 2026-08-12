"""Provider-specific tests for the shared Anthropic Messages runtime."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from anthropic import Anthropic
from pydantic import ValidationError

from neil_agent.config import Settings
from neil_agent.providers import anthropic_runtime
from neil_agent.providers.base import ProviderId, ProviderTurnState
from neil_agent.providers.claude import ClaudeProvider
from neil_agent.providers.deepseek import DeepSeekProvider
from neil_agent.providers.errors import ProviderProtocolError
from neil_agent.providers.factory import create_provider
from neil_agent.schemas import Message, ModelResponse, ToolCall


def claude_settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "llm_provider": ProviderId.CLAUDE,
        "llm_model": "claude-test-model",
        "anthropic_api_key": "claude-test-key",
    }
    values.update(updates)
    return Settings(**values)  # type: ignore[arg-type]


def deepseek_settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "deepseek_api_key": "deepseek-test-key",
    }
    values.update(updates)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.online
def test_claude_online_complete_smoke() -> None:
    if os.environ.get("NEIL_AGENT_RUN_CLAUDE_SMOKE") != "1":
        pytest.skip("set NEIL_AGENT_RUN_CLAUDE_SMOKE=1 to allow a paid API request")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    model_name = os.environ.get("NEIL_AGENT_CLAUDE_SMOKE_MODEL")
    if not api_key or not model_name:
        pytest.skip("ANTHROPIC_API_KEY and NEIL_AGENT_CLAUDE_SMOKE_MODEL are required")

    model = ClaudeProvider(
        Settings(
            _env_file=None,
            llm_provider=ProviderId.CLAUDE,
            llm_model=model_name,
            anthropic_api_key=api_key,
            max_tokens=32,
            max_retries=0,
        )
    )

    result = model.complete(
        [Message(role="user", content="Reply with exactly: OK")],
        system_prompt="Follow the user instruction exactly.",
    )

    assert result.strip()
    assert model.last_usage is not None


def test_claude_default_client_pins_native_endpoint_and_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=Anthropic)
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(anthropic_runtime, "Anthropic", factory)

    ClaudeProvider(claude_settings())

    kwargs = factory.call_args.kwargs
    assert kwargs["api_key"] == "claude-test-key"
    assert kwargs["max_retries"] == 0
    assert kwargs["base_url"] == "https://api.anthropic.com"


def test_claude_explicit_endpoint_override_is_forwarded_without_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = MagicMock(return_value=MagicMock(spec=Anthropic))
    monkeypatch.setattr(anthropic_runtime, "Anthropic", factory)

    ClaudeProvider(claude_settings(llm_base_url="https://gateway.example.test/v1/"))

    assert factory.call_args.kwargs["base_url"] == "https://gateway.example.test/v1"


def test_claude_thinking_modes_are_explicit_provider_differences() -> None:
    client = MagicMock(spec=Anthropic)
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="done")]
    )

    adaptive = ClaudeProvider(
        claude_settings(thinking_enabled=True),
        client=cast(Anthropic, client),
    )
    adaptive.complete([Message(role="user", content="work")], system_prompt="test")
    assert client.messages.create.call_args.kwargs["thinking"] == {"type": "adaptive"}

    manual = ClaudeProvider(
        claude_settings(
            thinking_enabled=True,
            claude_thinking_mode="enabled",
            claude_thinking_budget_tokens=2048,
            max_tokens=4096,
        ),
        client=cast(Anthropic, client),
    )
    manual.complete([Message(role="user", content="work")], system_prompt="test")
    assert client.messages.create.call_args.kwargs["thinking"] == {
        "type": "enabled",
        "budget_tokens": 2048,
    }


def test_claude_manual_thinking_budget_must_fit_output_limit() -> None:
    with pytest.raises(ValidationError, match="thinking budget"):
        claude_settings(
            thinking_enabled=True,
            claude_thinking_mode="enabled",
            claude_thinking_budget_tokens=2048,
            max_tokens=2048,
        )

    with pytest.raises(ValidationError, match="at least 1024"):
        claude_settings(
            thinking_enabled=True,
            claude_thinking_mode="enabled",
            claude_thinking_budget_tokens=512,
            max_tokens=2048,
        )


def test_irrelevant_claude_manual_budget_does_not_block_deepseek() -> None:
    settings = deepseek_settings(claude_thinking_budget_tokens=0)

    assert settings.llm_provider is ProviderId.DEEPSEEK


@pytest.mark.parametrize(
    ("state_provider", "state_model"),
    [
        (ProviderId.CLAUDE, "shared-model"),
        (ProviderId.DEEPSEEK, "different-model"),
    ],
)
def test_private_state_cannot_cross_provider_or_model(
    state_provider: ProviderId,
    state_model: str,
) -> None:
    client = MagicMock(spec=Anthropic)
    model = DeepSeekProvider(
        deepseek_settings(llm_model="shared-model"),
        client=cast(Anthropic, client),
    )
    state = ProviderTurnState(
        provider=state_provider,
        model=state_model,
        schema_version=1,
        payload={
            "thinking_blocks": (
                {"type": "thinking", "thinking": "x", "signature": "sig"},
            )
        },
    )
    assistant = Message(
        role="assistant",
        tool_calls=(ToolCall(id="call-1", name="read_file", arguments={}),),
        provider_state=state,
    )

    with pytest.raises(ProviderProtocolError, match="across providers or models"):
        model.complete(
            [assistant],
            system_prompt="test",
        )

    client.messages.create.assert_not_called()


def test_private_state_must_match_public_tool_calls() -> None:
    client = MagicMock(spec=Anthropic)
    model = ClaudeProvider(
        claude_settings(),
        client=cast(Anthropic, client),
    )
    state = ProviderTurnState(
        provider=ProviderId.CLAUDE,
        model="claude-test-model",
        schema_version=1,
        payload={
            "content_blocks": (
                {
                    "type": "tool_use",
                    "id": "hidden-id",
                    "name": "read_file",
                    "input": {},
                },
            )
        },
    )
    assistant = Message(
        role="assistant",
        tool_calls=(ToolCall(id="public-id", name="read_file", arguments={}),),
        provider_state=state,
    )

    with pytest.raises(ProviderProtocolError, match="does not match"):
        model.complete([assistant], system_prompt="test")

    client.messages.create.assert_not_called()


def test_redacted_thinking_state_survives_message_json_round_trip() -> None:
    state = ProviderTurnState(
        provider=ProviderId.CLAUDE,
        model="claude-test-model",
        schema_version=1,
        payload={
            "content_blocks": (
                {
                    "type": "redacted_thinking",
                    "data": "sanitized-encrypted-value",
                },
                {"type": "tool_use", "id": "toolu-1", "name": "read_file", "input": {}},
            )
        },
    )
    message = Message(
        role="assistant",
        tool_calls=(ToolCall(id="toolu-1", name="read_file", arguments={}),),
        provider_state=state,
    )

    restored = Message.model_validate_json(message.model_dump_json())

    assert restored.provider_state is not None
    assert restored.provider_state.belongs_to(
        ProviderId.CLAUDE,
        "claude-test-model",
    )
    assert restored.provider_state.payload["content_blocks"] == (
        {
            "type": "redacted_thinking",
            "data": "sanitized-encrypted-value",
        },
        {"type": "tool_use", "id": "toolu-1", "name": "read_file", "input": {}},
    )


def test_stream_close_cancels_transport_without_retry_or_terminal_response() -> None:
    client = MagicMock(spec=Anthropic)
    stream = MagicMock()
    stream.text_stream = iter(["first", "second"])
    manager = MagicMock()
    manager.__enter__.return_value = stream
    manager.__exit__.return_value = False
    client.messages.stream.return_value = manager
    model = ClaudeProvider(
        claude_settings(),
        client=cast(Anthropic, client),
    )

    events = model.stream(
        [Message(role="user", content="work")],
        system_prompt="test",
    )
    assert next(events) == "first"

    events.close()

    manager.__exit__.assert_called_once()
    assert manager.__exit__.call_args.args[0] is GeneratorExit
    stream.get_final_message.assert_not_called()
    assert client.messages.stream.call_count == 1
    assert model.last_usage is None


def test_duplicate_tool_ids_fail_closed() -> None:
    client = MagicMock(spec=Anthropic)
    stream = MagicMock()
    stream.text_stream = iter([])
    stream.get_final_message.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(type="tool_use", id="toolu-1", name="a", input={}),
            SimpleNamespace(type="tool_use", id="toolu-1", name="b", input={}),
        ],
        stop_reason="tool_use",
    )
    manager = MagicMock()
    manager.__enter__.return_value = stream
    client.messages.stream.return_value = manager
    model = ClaudeProvider(
        claude_settings(),
        client=cast(Anthropic, client),
    )

    with pytest.raises(ProviderProtocolError, match="重复的工具调用 ID"):
        tuple(
            model.stream(
                [Message(role="user", content="work")],
                system_prompt="test",
            )
        )


def test_unknown_anthropic_content_block_fails_closed() -> None:
    client = MagicMock(spec=Anthropic)
    stream = MagicMock()
    stream.text_stream = iter([])
    stream.get_final_message.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="future_block")],
        stop_reason="end_turn",
    )
    manager = MagicMock()
    manager.__enter__.return_value = stream
    client.messages.stream.return_value = manager
    model = ClaudeProvider(
        claude_settings(),
        client=cast(Anthropic, client),
    )

    with pytest.raises(ProviderProtocolError, match="future_block"):
        tuple(
            model.stream(
                [Message(role="user", content="work")],
                system_prompt="test",
            )
        )


def test_nonstreaming_tool_response_is_not_silently_flattened_to_text() -> None:
    client = MagicMock(spec=Anthropic)
    client.messages.create.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="partial"),
            SimpleNamespace(type="tool_use", id="toolu-1", name="read", input={}),
        ],
        usage=SimpleNamespace(input_tokens=4, output_tokens=2),
    )
    model = ClaudeProvider(
        claude_settings(),
        client=cast(Anthropic, client),
    )

    with pytest.raises(ProviderProtocolError, match="tool_use"):
        model.complete(
            [Message(role="user", content="work")],
            system_prompt="test",
        )
    assert model.last_usage is None


def test_malformed_tool_arguments_are_normalized_to_protocol_error() -> None:
    client = MagicMock(spec=Anthropic)
    stream = MagicMock()
    stream.text_stream = iter([])
    stream.get_final_message.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                id="toolu-1",
                name="read",
                input="not-an-object",
            )
        ],
        stop_reason="tool_use",
    )
    manager = MagicMock()
    manager.__enter__.return_value = stream
    client.messages.stream.return_value = manager
    model = ClaudeProvider(
        claude_settings(),
        client=cast(Anthropic, client),
    )

    with pytest.raises(ProviderProtocolError, match="工具参数对象"):
        tuple(
            model.stream(
                [Message(role="user", content="work")],
                system_prompt="test",
            )
        )


def test_default_factory_selects_claude_adapter() -> None:
    builder = MagicMock(return_value=MagicMock())
    settings = claude_settings()

    result = create_provider(settings, claude_builder=builder)

    assert result is builder.return_value
    builder.assert_called_once_with(settings, retry_handler=None)


def test_anthropic_provider_descriptors_are_distinct() -> None:
    deepseek = DeepSeekProvider(
        deepseek_settings(),
        client=cast(Anthropic, MagicMock(spec=Anthropic)),
    )
    claude = ClaudeProvider(
        claude_settings(),
        client=cast(Anthropic, MagicMock(spec=Anthropic)),
    )

    assert deepseek.descriptor.provider is ProviderId.DEEPSEEK
    assert claude.descriptor.provider is ProviderId.CLAUDE
    assert deepseek.descriptor is not claude.descriptor
    assert claude.descriptor.capabilities.reasoning_state is True
    assert claude.descriptor.capabilities.parallel_tool_calls is True


def test_tool_response_exposes_bound_private_state() -> None:
    client = MagicMock(spec=Anthropic)
    stream = MagicMock()
    stream.text_stream = iter([])
    stream.get_final_message.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="thinking",
                thinking="inspect",
                signature="sanitized-signature",
            ),
            SimpleNamespace(type="tool_use", id="toolu-1", name="read", input={}),
        ],
        stop_reason="tool_use",
    )
    manager = MagicMock()
    manager.__enter__.return_value = stream
    client.messages.stream.return_value = manager
    model = ClaudeProvider(
        claude_settings(),
        client=cast(Anthropic, client),
    )

    response = tuple(
        model.stream(
            [Message(role="user", content="work")],
            system_prompt="test",
        )
    )[-1]

    assert isinstance(response, ModelResponse)
    assert response.provider_state is not None
    assert response.provider_state.belongs_to(
        ProviderId.CLAUDE,
        "claude-test-model",
    )
