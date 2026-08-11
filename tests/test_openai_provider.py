"""Provider-specific tests for the native OpenAI Responses runtime."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest
from openai import BadRequestError, OpenAI, RateLimitError
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseReasoningItem,
)

from neil_agent.config import Settings
from neil_agent.providers import openai as openai_runtime
from neil_agent.providers.base import ProviderId, ProviderTurnState, StopReason
from neil_agent.providers.errors import (
    ProviderContextOverflowError,
    ProviderInternalError,
    ProviderProtocolError,
    ProviderRateLimitError,
)
from neil_agent.providers.factory import create_provider
from neil_agent.providers.openai import OpenAIProvider
from neil_agent.providers.openai_responses import (
    encode_messages,
    normalize_openai_error,
    parse_response,
)
from neil_agent.schemas import Message, ModelResponse, ToolCall, ToolResult


def openai_settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "llm_provider": ProviderId.OPENAI,
        "llm_model": "openai-test-model",
        "openai_api_key": "openai-test-key",
    }
    values.update(updates)
    return Settings(**values)  # type: ignore[arg-type]


def text_response(text: str = "done") -> dict[str, object]:
    return {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 3,
            "output_tokens": 1,
            "input_tokens_details": {
                "cached_tokens": 0,
                "cache_write_tokens": 0,
            },
        },
    }


def text_stream(text: str = "done") -> list[dict[str, object]]:
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {"id": "resp_1", "status": "in_progress"},
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "sequence_number": 2,
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.completed",
            "sequence_number": 3,
            "response": text_response(text),
        },
    ]


def stream_manager(events: Iterator[dict[str, object]]) -> MagicMock:
    manager = MagicMock()
    manager.__enter__.return_value = events
    manager.__exit__.return_value = False
    return manager


def test_default_client_pins_openai_endpoint_and_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=OpenAI)
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(openai_runtime, "OpenAI", factory)

    OpenAIProvider(openai_settings())

    assert factory.call_args.kwargs == {
        "api_key": "openai-test-key",
        "base_url": "https://api.openai.com/v1",
        "timeout": 120.0,
        "max_retries": 0,
    }


def test_explicit_endpoint_and_reasoning_effort_are_forwarded() -> None:
    client = MagicMock(spec=OpenAI)
    client.responses.create.return_value = text_response()
    model = OpenAIProvider(
        openai_settings(
            llm_base_url="https://gateway.example.test/openai/v1/",
            thinking_enabled=True,
            openai_reasoning_effort="high",
        ),
        client=cast(OpenAI, client),
    )

    assert (
        model.complete(
            [Message(role="user", content="work")],
            system_prompt="test",
        )
        == "done"
    )

    request = client.responses.create.call_args.kwargs
    assert request["reasoning"] == {"effort": "high"}
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["store"] is False
    assert model._base_url() == "https://gateway.example.test/openai/v1"


def test_function_output_preserves_call_id_and_marks_local_errors() -> None:
    messages = [
        Message(
            role="user",
            tool_results=(
                ToolResult(
                    tool_call_id="call_1",
                    content="permission denied",
                    is_error=True,
                ),
            ),
        )
    ]

    assert encode_messages(messages, model="openai-test-model") == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "Tool execution failed:\npermission denied",
        }
    ]


def test_incomplete_response_maps_max_output_tokens_without_treating_as_success() -> (
    None
):
    response = text_response("partial")
    response["status"] = "incomplete"
    response["incomplete_details"] = {"reason": "max_output_tokens"}

    result = parse_response(response, model="openai-test-model")

    assert result.content == "partial"
    assert result.stop_reason is StopReason.MAX_TOKENS


def test_refusal_maps_content_filter_and_preserves_visible_explanation() -> None:
    response = text_response()
    response["status"] = "incomplete"
    response["incomplete_details"] = {"reason": "content_filter"}
    response["output"] = [
        {
            "id": "msg_refusal_1",
            "type": "message",
            "role": "assistant",
            "status": "incomplete",
            "content": [{"type": "refusal", "refusal": "I cannot help with that."}],
        }
    ]

    result = parse_response(response, model="openai-test-model")

    assert result.content == "I cannot help with that."
    assert result.stop_reason is StopReason.CONTENT_FILTER


def test_text_response_private_items_survive_json_round_trip_and_replay() -> None:
    response = text_response("answer")
    response["output"] = [
        {
            "id": "reasoning_1",
            "type": "reasoning",
            "summary": [],
            "encrypted_content": "sanitized-encrypted-content",
            "status": "completed",
        },
        *cast(list[dict[str, object]], response["output"]),
    ]
    result = parse_response(response, model="openai-test-model")
    assert result.provider_state is not None
    message = Message(
        role="assistant",
        content=result.content,
        provider_state=result.provider_state,
    )

    restored = Message.model_validate_json(message.model_dump_json())

    assert restored.provider_state is not None
    assert encode_messages([restored], model="openai-test-model") == response["output"]


@pytest.mark.parametrize(
    ("state_provider", "state_model"),
    [
        (ProviderId.CLAUDE, "openai-test-model"),
        (ProviderId.OPENAI, "different-model"),
    ],
)
def test_private_state_cannot_cross_provider_or_model(
    state_provider: ProviderId,
    state_model: str,
) -> None:
    client = MagicMock(spec=OpenAI)
    state = ProviderTurnState(
        provider=state_provider,
        model=state_model,
        schema_version=1,
        payload={"output_items": text_response()["output"]},
    )
    model = OpenAIProvider(openai_settings(), client=cast(OpenAI, client))

    with pytest.raises(ProviderProtocolError, match="across providers or models"):
        model.complete(
            [Message(role="assistant", content="done", provider_state=state)],
            system_prompt="test",
        )

    client.responses.create.assert_not_called()


def test_private_state_must_match_public_tool_calls() -> None:
    client = MagicMock(spec=OpenAI)
    state = ProviderTurnState(
        provider=ProviderId.OPENAI,
        model="openai-test-model",
        schema_version=1,
        payload={
            "output_items": (
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "hidden_id",
                    "name": "read_file",
                    "arguments": "{}",
                    "status": "completed",
                },
            )
        },
    )
    message = Message(
        role="assistant",
        tool_calls=(ToolCall(id="public_id", name="read_file", arguments={}),),
        provider_state=state,
    )
    model = OpenAIProvider(openai_settings(), client=cast(OpenAI, client))

    with pytest.raises(ProviderProtocolError, match="does not match"):
        model.complete([message], system_prompt="test")

    client.responses.create.assert_not_called()


def test_malformed_function_arguments_fail_closed() -> None:
    response = text_response()
    response["output"] = [
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": "{not-json",
            "status": "completed",
        }
    ]

    with pytest.raises(ProviderProtocolError, match="完整 JSON"):
        parse_response(response, model="openai-test-model")


@pytest.mark.parametrize(
    "arguments",
    [
        '{"value":NaN}',
        '{"path":"a","path":"b"}',
    ],
)
def test_ambiguous_or_nonstandard_function_json_fails_closed(arguments: str) -> None:
    response = text_response()
    response["output"] = [
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": arguments,
            "status": "completed",
        }
    ]

    with pytest.raises(ProviderProtocolError, match="完整 JSON"):
        parse_response(response, model="openai-test-model")


def test_reasoning_without_encrypted_content_fails_closed() -> None:
    response = text_response()
    response["output"] = [
        {"id": "reasoning_1", "type": "reasoning", "summary": []},
        *cast(list[dict[str, object]], response["output"]),
    ]

    with pytest.raises(ProviderProtocolError, match="encrypted reasoning"):
        parse_response(response, model="openai-test-model")


def test_real_sdk_output_models_are_decoded_and_preserved() -> None:
    response = {
        "status": "completed",
        "output": [
            ResponseReasoningItem.model_validate(
                {
                    "id": "reasoning_1",
                    "type": "reasoning",
                    "summary": [],
                    "encrypted_content": "sanitized-encrypted-content",
                    "status": "completed",
                }
            ),
            ResponseOutputMessage.model_validate(
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "inspect", "annotations": []}
                    ],
                }
            ),
            ResponseFunctionToolCall.model_validate(
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": '{"path":"README.md"}',
                    "status": "completed",
                }
            ),
        ],
        "usage": None,
    }

    result = parse_response(response, model="openai-test-model")

    assert result.content == "inspect"
    assert result.tool_calls == (
        ToolCall(id="call_1", name="read_file", arguments={"path": "README.md"}),
    )
    assert result.provider_state is not None
    assert result.provider_state.payload["output_items"][0]["type"] == "reasoning"


def test_openai_sdk_errors_are_normalized_with_retry_and_context_semantics() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    rate_response = httpx.Response(
        429,
        request=request,
        headers={"retry-after": "3"},
    )
    rate_error = RateLimitError(
        "sensitive upstream message",
        response=rate_response,
        body=None,
    )
    context_response = httpx.Response(400, request=request)
    context_error = BadRequestError(
        "sensitive prompt fragment",
        response=context_response,
        body={"code": "context_length_exceeded"},
    )

    normalized_rate = normalize_openai_error(rate_error)
    normalized_context = normalize_openai_error(context_error)

    assert isinstance(normalized_rate, ProviderRateLimitError)
    assert normalized_rate.retry_after == 3
    assert "sensitive" not in str(normalized_rate)
    assert isinstance(normalized_context, ProviderContextOverflowError)
    assert "prompt fragment" not in str(normalized_context)


def test_stream_sequence_and_terminal_text_are_validated() -> None:
    client = MagicMock(spec=OpenAI)
    events = text_stream()
    events[2]["sequence_number"] = 1
    client.responses.create.return_value = stream_manager(iter(events))
    model = OpenAIProvider(openai_settings(), client=cast(OpenAI, client))

    with pytest.raises(ProviderProtocolError, match="严格递增"):
        tuple(
            model.stream(
                [Message(role="user", content="work")],
                system_prompt="test",
            )
        )


def test_partial_function_argument_stream_is_never_retried() -> None:
    client = MagicMock(spec=OpenAI)
    events = [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {"id": "resp_1", "status": "in_progress"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "sequence_number": 1,
            "item_id": "fc_1",
            "output_index": 0,
            "delta": "{",
        },
        {"type": "error", "sequence_number": 2, "code": "server_error"},
    ]
    client.responses.create.return_value = stream_manager(iter(events))
    sleeper = MagicMock()
    model = OpenAIProvider(
        openai_settings(max_retries=2),
        client=cast(OpenAI, client),
        sleeper=sleeper,
    )

    with pytest.raises(ProviderInternalError):
        tuple(
            model.stream(
                [Message(role="user", content="work")],
                system_prompt="test",
            )
        )

    assert client.responses.create.call_count == 1
    sleeper.assert_not_called()


def test_failed_terminal_without_output_keeps_provider_internal_category() -> None:
    client = MagicMock(spec=OpenAI)
    events = [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {"id": "resp_1", "status": "in_progress"},
        },
        {
            "type": "response.failed",
            "sequence_number": 1,
            "response": {
                "id": "resp_1",
                "status": "failed",
                "output": None,
                "error": {"code": "server_error", "message": "upstream detail"},
            },
        },
    ]
    client.responses.create.return_value = stream_manager(iter(events))
    model = OpenAIProvider(
        openai_settings(max_retries=0),
        client=cast(OpenAI, client),
    )

    with pytest.raises(ProviderInternalError, match="服务端"):
        tuple(
            model.stream(
                [Message(role="user", content="work")],
                system_prompt="test",
            )
        )


def test_error_before_output_is_retried_with_a_fresh_stream() -> None:
    client = MagicMock(spec=OpenAI)
    failed = stream_manager(
        iter(
            [
                {
                    "type": "response.created",
                    "sequence_number": 0,
                    "response": {"id": "resp_1", "status": "in_progress"},
                },
                {
                    "type": "error",
                    "sequence_number": 1,
                    "code": "server_error",
                },
            ]
        )
    )
    succeeded = stream_manager(iter(text_stream("recovered")))
    client.responses.create.side_effect = [failed, succeeded]
    sleeper = MagicMock()
    model = OpenAIProvider(
        openai_settings(max_retries=1, retry_base_delay=0.25),
        client=cast(OpenAI, client),
        sleeper=sleeper,
    )

    events = tuple(
        model.stream(
            [Message(role="user", content="work")],
            system_prompt="test",
        )
    )

    assert events[0] == "recovered"
    assert isinstance(events[-1], ModelResponse)
    assert client.responses.create.call_count == 2
    sleeper.assert_called_once_with(0.25)


def test_stream_close_releases_transport_without_terminal_or_usage() -> None:
    client = MagicMock(spec=OpenAI)
    manager = stream_manager(iter(text_stream("first")))
    client.responses.create.return_value = manager
    model = OpenAIProvider(openai_settings(), client=cast(OpenAI, client))
    events = model.stream(
        [Message(role="user", content="work")],
        system_prompt="test",
    )

    assert next(events) == "first"
    events.close()

    manager.__exit__.assert_called_once()
    assert manager.__exit__.call_args.args[0] is GeneratorExit
    assert model.last_usage is None
    assert client.responses.create.call_count == 1


def test_default_factory_selects_openai_adapter() -> None:
    builder = MagicMock(return_value=MagicMock())
    settings = openai_settings()

    result = create_provider(settings, openai_builder=builder)

    assert result is builder.return_value
    builder.assert_called_once_with(settings, retry_handler=None)


@pytest.mark.online
def test_openai_online_complete_smoke() -> None:
    if os.environ.get("NEIL_AGENT_RUN_OPENAI_SMOKE") != "1":
        pytest.skip("set NEIL_AGENT_RUN_OPENAI_SMOKE=1 to allow a paid API request")
    api_key = os.environ.get("OPENAI_API_KEY")
    model_name = os.environ.get("NEIL_AGENT_OPENAI_SMOKE_MODEL")
    if not api_key or not model_name:
        pytest.skip("OPENAI_API_KEY and NEIL_AGENT_OPENAI_SMOKE_MODEL are required")

    model = OpenAIProvider(
        Settings(
            _env_file=None,
            llm_provider=ProviderId.OPENAI,
            llm_model=model_name,
            openai_api_key=api_key,
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
