"""Tests for provider-neutral contracts, retry policy, and factory behavior."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence

import pytest

from neil_agent.config import Settings
from neil_agent.errors import LLMError
from neil_agent.providers.anthropic_messages import (
    encode_message,
    encode_tool,
    normalize_stop_reason,
)
from neil_agent.providers.base import (
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderId,
    ProviderTurnState,
    StopReason,
    WireProtocol,
)
from neil_agent.providers.errors import (
    ProviderAuthenticationError,
    ProviderErrorCategory,
    ProviderNotImplementedError,
    ProviderRateLimitError,
)
from neil_agent.providers.factory import ProviderFactory
from neil_agent.providers.retry import RetryPolicy, parse_retry_after
from neil_agent.schemas import (
    ActivityEvent,
    Message,
    ModelResponse,
    ThinkingContent,
    ToolCall,
    ToolDefinition,
    ToolResult,
)


class FakeProviderModel:
    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        return "complete"

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        yield ModelResponse(content="complete")


def _capabilities() -> ProviderCapabilities:
    return ProviderCapabilities(
        streaming=True,
        tool_calling=True,
        parallel_tool_calls=False,
        reasoning_state=True,
        structured_output=False,
        usage_reporting=True,
        prompt_caching=False,
    )


def test_provider_descriptor_and_stop_reasons_are_stable() -> None:
    descriptor = ProviderDescriptor(
        provider=ProviderId.DEEPSEEK,
        display_name="DeepSeek",
        wire_protocol=WireProtocol.ANTHROPIC_MESSAGES,
        capabilities=_capabilities(),
    )

    assert descriptor.provider.value == "deepseek"
    assert descriptor.capabilities.streaming is True
    assert tuple(reason.value for reason in StopReason) == (
        "end_turn",
        "tool_call",
        "max_tokens",
        "content_filter",
        "cancelled",
        "error",
        "unknown",
    )
    with pytest.raises(ValueError, match="display name"):
        ProviderDescriptor(
            provider=ProviderId.DEEPSEEK,
            display_name="   ",
            wire_protocol=WireProtocol.ANTHROPIC_MESSAGES,
            capabilities=_capabilities(),
        )


def test_provider_turn_state_is_copied_frozen_and_model_bound() -> None:
    source = {"response_id": "response-1"}
    state = ProviderTurnState(
        provider=ProviderId.OPENAI,
        model="configured-model",
        schema_version=1,
        payload=source,
    )
    source["response_id"] = "changed"

    assert state.payload["response_id"] == "response-1"
    assert "response-1" not in repr(state)
    assert state.belongs_to(ProviderId.OPENAI, "configured-model") is True
    assert state.belongs_to(ProviderId.CLAUDE, "configured-model") is False
    response = ModelResponse(content="done", provider_state=state)
    assert response.provider_state == state
    assert response.model_dump(mode="json")["provider_state"] == {
        "provider": "openai",
        "model": "configured-model",
        "schema_version": 1,
        "payload": {"response_id": "response-1"},
    }
    with pytest.raises(TypeError):
        state.payload["response_id"] = "forbidden"  # type: ignore[index]
    with pytest.raises(ValueError, match="schema version"):
        ProviderTurnState(
            provider=ProviderId.OPENAI,
            model="configured-model",
            schema_version=0,
            payload={},
        )


def test_anthropic_stop_reason_mapping_fails_closed_for_unknown_values() -> None:
    assert (
        normalize_stop_reason("end_turn", has_tool_calls=False) is StopReason.END_TURN
    )
    assert (
        normalize_stop_reason("tool_use", has_tool_calls=False) is StopReason.TOOL_CALL
    )
    assert normalize_stop_reason(None, has_tool_calls=True) is StopReason.TOOL_CALL
    assert (
        normalize_stop_reason("max_tokens", has_tool_calls=False)
        is StopReason.MAX_TOKENS
    )
    assert (
        normalize_stop_reason("new-provider-value", has_tool_calls=False)
        is StopReason.UNKNOWN
    )


def test_retry_policy_is_bounded_and_stops_after_output() -> None:
    policy = RetryPolicy(max_retries=2, base_delay=0.5, max_delay=3.0)
    rate_limit = ProviderRateLimitError(
        "limited",
        provider=ProviderId.DEEPSEEK,
        retry_after=30.0,
    )
    authentication = ProviderAuthenticationError(
        "invalid key",
        provider=ProviderId.DEEPSEEK,
    )

    assert policy.can_retry(rate_limit, 0) is True
    assert policy.can_retry(rate_limit, 0, output_started=True) is False
    assert policy.can_retry(rate_limit, 2) is False
    assert policy.can_retry(authentication, 0) is False
    assert policy.delay(rate_limit, 1) == 3.0
    assert (
        policy.delay(
            ProviderRateLimitError("limited", provider=ProviderId.DEEPSEEK),
            3,
        )
        == 2.0
    )
    assert rate_limit.category is ProviderErrorCategory.RATE_LIMIT
    assert isinstance(rate_limit, LLMError)


def test_retry_after_parser_prefers_milliseconds_and_rejects_non_finite() -> None:
    assert parse_retry_after({"Retry-After-MS": "1500", "retry-after": "9"}) == 1.5
    assert parse_retry_after({"retry-after-ms": "invalid", "retry-after": "2"}) == 2
    assert parse_retry_after({"retry-after": "inf"}) is None
    assert parse_retry_after({"retry-after": "-1"}) is None


def test_anthropic_serialization_is_owned_by_the_adapter() -> None:
    assistant = Message(
        role="assistant",
        content="checking",
        thinking=(ThinkingContent(thinking="inspect", signature="sig"),),
        tool_calls=(
            ToolCall(
                id="call-1",
                name="read_file",
                arguments={"path": "README.md"},
            ),
        ),
    )
    result = Message(
        role="user",
        tool_results=(
            ToolResult(tool_call_id="call-1", content="contents", is_error=False),
        ),
    )
    tool = ToolDefinition(
        name="read_file",
        description="Read a file.",
        input_schema={"type": "object"},
    )

    assert encode_message(assistant) == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "inspect", "signature": "sig"},
            {"type": "text", "text": "checking"},
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "read_file",
                "input": {"path": "README.md"},
            },
        ],
    }
    assert encode_message(result) == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call-1",
                "content": "contents",
                "is_error": False,
            }
        ],
    }
    assert encode_tool(tool) == {
        "name": "read_file",
        "description": "Read a file.",
        "input_schema": {"type": "object"},
    }


def test_provider_factory_builds_only_registered_provider() -> None:
    observed: list[tuple[ProviderId, object | None]] = []

    def build(
        settings: Settings,
        *,
        retry_handler: Callable[[ActivityEvent], None] | None = None,
    ) -> FakeProviderModel:
        observed.append((settings.llm_provider, retry_handler))
        return FakeProviderModel()

    factory = ProviderFactory({ProviderId.DEEPSEEK: build})

    def handler(_event: ActivityEvent) -> None:
        return None

    settings = Settings(_env_file=None, deepseek_api_key="test-key")

    model = factory.create(settings, retry_handler=handler)

    assert isinstance(model, FakeProviderModel)
    assert factory.registered_providers == (ProviderId.DEEPSEEK,)
    assert observed == [(ProviderId.DEEPSEEK, handler)]


def test_provider_factory_rejects_unimplemented_provider_before_builder() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider=ProviderId.CLAUDE,
        llm_model="configured-claude-model",
        anthropic_api_key="claude-key",
    )
    factory = ProviderFactory({})

    with pytest.raises(ProviderNotImplementedError, match="claude") as error_info:
        factory.create(settings)

    assert error_info.value.provider is ProviderId.CLAUDE
    assert error_info.value.retryable is False
