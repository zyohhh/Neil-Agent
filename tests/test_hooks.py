"""Tests for bounded, typed lifecycle hooks."""

from collections.abc import Iterator, Sequence

import pytest

from neil_agent.agent import Agent
from neil_agent.errors import HookError
from neil_agent.hooks import HookEvent, HookResponse, LifecycleHooks
from neil_agent.schemas import Message, ModelResponse, ToolCall, ToolDefinition
from neil_agent.tools.registry import ToolRegistry


class HookFakeModel:
    def __init__(self, responses: list[list[str | ModelResponse]]) -> None:
        self.responses = responses
        self.system_prompts: list[str] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        raise NotImplementedError

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self.system_prompts.append(system_prompt)
        yield from self.responses.pop(0)


def test_before_model_hook_adds_bounded_request_only_context() -> None:
    hooks = LifecycleHooks()
    hooks.register(
        "before_model",
        lambda event: HookResponse(additional_context="LOCAL-POLICY"),
    )
    model = HookFakeModel([["done", ModelResponse(content="done")]])
    agent = Agent(model, system_prompt="BASE", hooks=hooks)

    response = "".join(agent.stream_chat("hello"))

    assert response == "done"
    assert "LOCAL-POLICY" in model.system_prompts[0]
    assert "LOCAL-POLICY" not in " ".join(
        message.content for message in agent.messages
    )


def test_after_model_audit_receives_typed_response() -> None:
    observed: list[ModelResponse | None] = []
    hooks = LifecycleHooks()
    hooks.register(
        "after_model",
        lambda event: observed.append(event.model_response) or None,
    )
    model = HookFakeModel([["done", ModelResponse(content="done")]])

    list(Agent(model, hooks=hooks).stream_chat("hello"))

    assert observed == [ModelResponse(content="done")]


def test_before_tool_hook_denies_without_calling_handler() -> None:
    calls: list[str] = []

    def read_value() -> str:
        calls.append("executed")
        return "value"

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read_value",
            description="Read one test value.",
            input_schema={"type": "object", "additionalProperties": False},
        ),
        read_value,
    )
    hooks = LifecycleHooks()
    hooks.register(
        "before_tool",
        lambda event: HookResponse(decision="deny", reason="blocked by policy"),
    )
    call = ToolCall(id="call-1", name="read_value")
    model = HookFakeModel(
        [
            [ModelResponse(tool_calls=(call,))],
            ["stopped", ModelResponse(content="stopped")],
        ]
    )
    agent = Agent(model, registry=registry, hooks=hooks)

    assert "".join(agent.stream_chat("read it")) == "stopped"
    assert calls == []
    result = agent.messages[2].tool_results[0]
    assert result.is_error is True
    assert "blocked by policy" in result.content


def test_audit_hook_cannot_deny_or_add_context() -> None:
    hooks = LifecycleHooks()
    hooks.register(
        "after_model",
        lambda event: HookResponse(decision="deny", reason="not allowed"),
    )

    with pytest.raises(HookError, match="只读审计阶段"):
        hooks.dispatch(HookEvent(stage="after_model"))


def test_hook_rejects_invalid_stage_and_oversized_context() -> None:
    hooks = LifecycleHooks()
    with pytest.raises(HookError, match="未知"):
        hooks.register("invalid", lambda event: None)  # type: ignore[arg-type]

    hooks.register(
        "before_model",
        lambda event: HookResponse(additional_context="x" * 2_001),
    )
    with pytest.raises(HookError, match="2000"):
        hooks.dispatch(HookEvent(stage="before_model"))
