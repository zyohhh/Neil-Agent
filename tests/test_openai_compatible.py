"""Contract and profile tests for local OpenAI-compatible servers."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from openai import OpenAI
from pydantic import ValidationError

from neil_agent.config import Settings
from neil_agent.providers.base import ProviderId, ProviderTurnState, WireProtocol
from neil_agent.providers.errors import (
    ProviderProtocolError,
    UnsupportedCapabilityError,
)
from neil_agent.providers.factory import create_provider
from neil_agent.providers.openai_compatible import OllamaProvider, VLLMProvider
from neil_agent.schemas import (
    Message,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    ToolResult,
)

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "providers"
    / "openai_compatible_responses_v1.json"
)


def local_settings(provider: ProviderId, **updates: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "llm_provider": provider,
        "llm_model": "local-test-model",
        "max_tokens": 256,
    }
    values.update(updates)
    return Settings(**values)  # type: ignore[arg-type]


def fixture() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(FIXTURE_PATH.read_text(encoding="utf-8")),
    )


def mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def items(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    assert all(isinstance(item, dict) for item in value)
    return cast(list[dict[str, object]], value)


def stream_manager(events: Iterator[dict[str, object]]) -> MagicMock:
    manager = MagicMock()
    manager.__enter__.return_value = events
    manager.__exit__.return_value = False
    return manager


@pytest.mark.parametrize(
    ("provider_id", "provider_type"),
    [
        (ProviderId.OLLAMA, OllamaProvider),
        (ProviderId.VLLM, VLLMProvider),
    ],
)
def test_compatible_fixture_complete_and_stream_contract(
    provider_id: ProviderId,
    provider_type: type[OllamaProvider] | type[VLLMProvider],
) -> None:
    data = fixture()
    profile = mapping(mapping(data["providers"])[provider_id.value])
    cases = mapping(data["cases"])
    complete = mapping(cases["complete_text"])
    complete_input = mapping(complete["input"])
    client = MagicMock(spec=OpenAI)
    client.responses.create.return_value = complete["wire_response"]
    model = provider_type(
        local_settings(
            provider_id,
            llm_model=profile["model"],
            llm_base_url=profile["base_url"],
        ),
        client=cast(OpenAI, client),
    )

    result = model.complete(
        [Message.model_validate(item) for item in items(complete_input["messages"])],
        system_prompt=cast(str, complete_input["system_prompt"]),
    )

    assert result == mapping(complete["contract"])["result"]
    expected_request = {"model": profile["model"], **mapping(complete["wire_request"])}
    assert client.responses.create.call_args.kwargs == expected_request

    streamed = mapping(cases["stream_text"])
    streamed_input = mapping(streamed["input"])
    wire_stream = mapping(streamed["wire_stream"])
    manager = stream_manager(iter(items(wire_stream["events"])))
    client.responses.create.return_value = manager
    events = tuple(
        model.stream(
            [
                Message.model_validate(item)
                for item in items(streamed_input["messages"])
            ],
            system_prompt=cast(str, streamed_input["system_prompt"]),
        )
    )
    contract = mapping(streamed["contract"])

    assert list(events[:-1]) == contract["text_deltas"]
    assert events[-1] == ModelResponse.model_validate(contract["response"])
    expected_stream_request = {
        "model": profile["model"],
        **mapping(streamed["wire_request"]),
    }
    assert client.responses.create.call_args.kwargs == expected_stream_request


def test_fixture_is_versioned_synthetic_and_secret_free() -> None:
    raw = FIXTURE_PATH.read_text(encoding="utf-8")
    data = fixture()

    assert data["fixture_version"] == 1
    assert data["wire_protocol"] == "openai-compatible"
    assert mapping(data["source"]) == {
        "kind": "synthetic-characterization",
        "live_capture": False,
        "sanitized": True,
        "sdk": "openai",
        "sdk_version": "2.53.0",
    }
    assert "local_api_key" not in raw.lower()


@pytest.mark.parametrize(
    ("provider_id", "provider_type", "base_url", "placeholder"),
    [
        (
            ProviderId.OLLAMA,
            OllamaProvider,
            "http://localhost:11434/v1",
            "ollama",
        ),
        (ProviderId.VLLM, VLLMProvider, "http://localhost:8000/v1", "EMPTY"),
    ],
)
def test_local_client_uses_profile_endpoint_placeholder_and_no_sdk_retries(
    provider_id: ProviderId,
    provider_type: type[OllamaProvider] | type[VLLMProvider],
    base_url: str,
    placeholder: str,
) -> None:
    factory = MagicMock(return_value=MagicMock(spec=OpenAI))

    provider_type(local_settings(provider_id), client_factory=factory)

    assert factory.call_args.kwargs == {
        "api_key": placeholder,
        "base_url": base_url,
        "timeout": 120.0,
        "max_retries": 0,
    }


def test_local_api_key_and_custom_endpoint_are_forwarded_without_cloud_key() -> None:
    factory = MagicMock(return_value=MagicMock(spec=OpenAI))
    model = VLLMProvider(
        local_settings(
            ProviderId.VLLM,
            llm_base_url="http://127.0.0.1:9000/custom/v1/",
            local_api_key="private-local-token",
            request_timeout=3.5,
        ),
        client_factory=factory,
    )

    assert factory.call_args.kwargs == {
        "api_key": "private-local-token",
        "base_url": "http://127.0.0.1:9000/custom/v1",
        "timeout": 3.5,
        "max_retries": 0,
    }
    assert model.settings.openai_api_key is None
    assert "private-local-token" not in repr(model.settings)


def test_local_endpoint_requires_v1_path_without_query_or_fragment() -> None:
    with pytest.raises(ValidationError, match="must end with /v1"):
        local_settings(
            ProviderId.OLLAMA,
            llm_base_url="http://localhost:11434/api",
        )

    with pytest.raises(ValidationError, match="cannot use query or fragment"):
        local_settings(
            ProviderId.VLLM,
            llm_base_url="http://localhost:8000/v1?tenant=test",
        )


@pytest.mark.parametrize(
    ("provider_id", "provider_type"),
    [
        (ProviderId.OLLAMA, OllamaProvider),
        (ProviderId.VLLM, VLLMProvider),
    ],
)
def test_tools_are_rejected_before_network_without_explicit_model_capability(
    provider_id: ProviderId,
    provider_type: type[OllamaProvider] | type[VLLMProvider],
) -> None:
    client = MagicMock(spec=OpenAI)
    model = provider_type(
        local_settings(provider_id),
        client=cast(OpenAI, client),
    )
    tool = ToolDefinition(
        name="read_file",
        description="Read one file.",
        input_schema={"type": "object", "properties": {}},
    )

    with pytest.raises(UnsupportedCapabilityError, match="未显式启用工具"):
        tuple(
            model.stream(
                [Message(role="user", content="work")],
                system_prompt="test",
                tools=[tool],
            )
        )

    client.responses.create.assert_not_called()


@pytest.mark.parametrize(
    ("provider_id", "provider_type"),
    [
        (ProviderId.OLLAMA, OllamaProvider),
        (ProviderId.VLLM, VLLMProvider),
    ],
)
def test_tool_history_is_rejected_when_local_tool_capability_is_disabled(
    provider_id: ProviderId,
    provider_type: type[OllamaProvider] | type[VLLMProvider],
) -> None:
    client = MagicMock(spec=OpenAI)
    model = provider_type(
        local_settings(provider_id),
        client=cast(OpenAI, client),
    )
    messages = [
        Message(role="user", content="work"),
        Message(
            role="assistant",
            tool_calls=(ToolCall(id="call_1", name="read_file"),),
        ),
        Message(
            role="user",
            tool_results=(ToolResult(tool_call_id="call_1", content="data"),),
        ),
    ]

    with pytest.raises(UnsupportedCapabilityError, match="未显式启用工具调用"):
        model.complete(messages, system_prompt="test")

    client.responses.create.assert_not_called()


def test_local_profile_rejects_private_turn_state_before_network() -> None:
    client = MagicMock(spec=OpenAI)
    model = OllamaProvider(
        local_settings(ProviderId.OLLAMA),
        client=cast(OpenAI, client),
    )
    private_state = ProviderTurnState(
        provider=ProviderId.OLLAMA,
        model="local-test-model",
        schema_version=1,
        payload={"output_items": ()},
    )

    with pytest.raises(UnsupportedCapabilityError, match="私有 turn state"):
        model.complete(
            [
                Message(role="user", content="work"),
                Message(
                    role="assistant",
                    content="previous",
                    provider_state=private_state,
                ),
                Message(role="user", content="continue"),
            ],
            system_prompt="test",
        )

    client.responses.create.assert_not_called()


@pytest.mark.parametrize(
    ("provider_id", "provider_type"),
    [
        (ProviderId.OLLAMA, OllamaProvider),
        (ProviderId.VLLM, VLLMProvider),
    ],
)
def test_reasoning_is_rejected_before_network_for_conservative_local_profiles(
    provider_id: ProviderId,
    provider_type: type[OllamaProvider] | type[VLLMProvider],
) -> None:
    client = MagicMock(spec=OpenAI)
    model = provider_type(
        local_settings(provider_id, thinking_enabled=True),
        client=cast(OpenAI, client),
    )

    with pytest.raises(UnsupportedCapabilityError, match="未启用 reasoning"):
        model.complete(
            [Message(role="user", content="work")],
            system_prompt="test",
        )

    client.responses.create.assert_not_called()


def test_vllm_verified_tool_profile_sends_explicit_parallel_policy() -> None:
    client = MagicMock(spec=OpenAI)
    final_response = {
        "status": "completed",
        "output": [
            {
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": "{}",
                "status": "completed",
            }
        ],
        "usage": None,
    }
    events = [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {"id": "resp_1", "status": "in_progress"},
        },
        {
            "type": "response.function_call_arguments.done",
            "sequence_number": 1,
            "item_id": "fc_1",
            "output_index": 0,
            "name": "read_file",
            "arguments": "{}",
        },
        {
            "type": "response.completed",
            "sequence_number": 2,
            "response": final_response,
        },
    ]
    client.responses.create.return_value = stream_manager(iter(events))
    model = VLLMProvider(
        local_settings(
            ProviderId.VLLM,
            local_tool_calling_enabled=True,
            local_parallel_tool_calls_enabled=False,
        ),
        client=cast(OpenAI, client),
    )
    tool = ToolDefinition(
        name="read_file",
        description="Read one file.",
        input_schema={"type": "object", "properties": {}},
    )

    result = tuple(
        model.stream(
            [Message(role="user", content="work")],
            system_prompt="test",
            tools=[tool],
        )
    )[-1]

    assert isinstance(result, ModelResponse)
    assert result.provider_state is None
    assert client.responses.create.call_args.kwargs["parallel_tool_calls"] is False


def test_ollama_parallel_tools_are_rejected_by_configuration() -> None:
    with pytest.raises(ValidationError, match="Ollama profile"):
        local_settings(
            ProviderId.OLLAMA,
            local_tool_calling_enabled=True,
            local_parallel_tool_calls_enabled=True,
        )


def test_local_capability_flags_do_not_invalidate_a_cloud_provider() -> None:
    settings = local_settings(
        ProviderId.OPENAI,
        openai_api_key="test-key",
        local_parallel_tool_calls_enabled=True,
    )

    assert settings.llm_provider is ProviderId.OPENAI


def test_reasoning_output_fails_closed_against_local_profile() -> None:
    reasoning_client = MagicMock(spec=OpenAI)
    reasoning_client.responses.create.return_value = {
        "status": "completed",
        "output": [
            {
                "id": "reasoning_1",
                "type": "reasoning",
                "summary": [],
                "encrypted_content": "opaque",
            },
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "done"}],
            },
        ],
        "usage": None,
    }
    model = OllamaProvider(
        local_settings(ProviderId.OLLAMA),
        client=cast(OpenAI, reasoning_client),
    )

    with pytest.raises(ProviderProtocolError, match="reasoning output") as info:
        model.complete(
            [Message(role="user", content="work")],
            system_prompt="test",
        )

    assert info.value.provider is ProviderId.OLLAMA


@pytest.mark.parametrize("parallel_enabled", [False, True])
def test_vllm_parallel_response_follows_explicit_capability(
    parallel_enabled: bool,
) -> None:
    client = MagicMock(spec=OpenAI)
    output = [
        {
            "id": f"fc_{index}",
            "type": "function_call",
            "call_id": f"call_{index}",
            "name": "read_file",
            "arguments": "{}",
            "status": "completed",
        }
        for index in (1, 2)
    ]
    events = [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {"id": "resp_parallel", "status": "in_progress"},
        },
        *[
            {
                "type": "response.function_call_arguments.done",
                "sequence_number": index,
                "item_id": f"fc_{index}",
                "output_index": index - 1,
                "name": "read_file",
                "arguments": "{}",
            }
            for index in (1, 2)
        ],
        {
            "type": "response.completed",
            "sequence_number": 3,
            "response": {
                "id": "resp_parallel",
                "status": "completed",
                "output": output,
                "usage": None,
            },
        },
    ]
    client.responses.create.return_value = stream_manager(iter(events))
    model = VLLMProvider(
        local_settings(
            ProviderId.VLLM,
            local_tool_calling_enabled=True,
            local_parallel_tool_calls_enabled=parallel_enabled,
        ),
        client=cast(OpenAI, client),
    )
    tool = ToolDefinition(
        name="read_file",
        description="Read one file.",
        input_schema={"type": "object", "properties": {}},
    )

    if not parallel_enabled:
        with pytest.raises(ProviderProtocolError, match="不允许并行"):
            tuple(
                model.stream(
                    [Message(role="user", content="work")],
                    system_prompt="test",
                    tools=[tool],
                )
            )
    else:
        result = tuple(
            model.stream(
                [Message(role="user", content="work")],
                system_prompt="test",
                tools=[tool],
            )
        )[-1]
        assert isinstance(result, ModelResponse)
        assert len(result.tool_calls) == 2

    assert (
        client.responses.create.call_args.kwargs["parallel_tool_calls"]
        is parallel_enabled
    )


def test_factory_registers_both_local_profiles() -> None:
    ollama_builder = MagicMock(return_value=MagicMock())
    vllm_builder = MagicMock(return_value=MagicMock())

    ollama = create_provider(
        local_settings(ProviderId.OLLAMA),
        ollama_builder=ollama_builder,
    )
    vllm = create_provider(
        local_settings(ProviderId.VLLM),
        vllm_builder=vllm_builder,
    )

    assert ollama is ollama_builder.return_value
    assert vllm is vllm_builder.return_value
    ollama_builder.assert_called_once()
    vllm_builder.assert_called_once()


@pytest.mark.online
@pytest.mark.parametrize(
    ("provider_id", "provider_type", "enabled_env", "model_env"),
    [
        (
            ProviderId.OLLAMA,
            OllamaProvider,
            "NEIL_AGENT_RUN_OLLAMA_SMOKE",
            "NEIL_AGENT_OLLAMA_SMOKE_MODEL",
        ),
        (
            ProviderId.VLLM,
            VLLMProvider,
            "NEIL_AGENT_RUN_VLLM_SMOKE",
            "NEIL_AGENT_VLLM_SMOKE_MODEL",
        ),
    ],
)
def test_local_provider_online_smoke(
    provider_id: ProviderId,
    provider_type: type[OllamaProvider] | type[VLLMProvider],
    enabled_env: str,
    model_env: str,
) -> None:
    if os.environ.get(enabled_env) != "1":
        pytest.skip(f"set {enabled_env}=1 to contact the local model server")
    model_name = os.environ.get(model_env)
    if not model_name:
        pytest.skip(f"{model_env} is required")

    model = provider_type(
        local_settings(
            provider_id,
            llm_model=model_name,
            max_tokens=32,
            max_retries=0,
        )
    )

    result = model.complete(
        [Message(role="user", content="Reply with exactly: OK")],
        system_prompt="Follow the instruction exactly.",
    )

    assert result.strip()
    assert model.descriptor.wire_protocol is WireProtocol.OPENAI_COMPATIBLE
