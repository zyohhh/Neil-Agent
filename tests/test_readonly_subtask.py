"""Tests for read-only subtask execution and profile wiring."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from threading import Event

import pytest

from neil_agent.agent import Agent
from neil_agent.config import Settings
from neil_agent.errors import ToolError
from neil_agent.events import EventBus, RuntimeEvent, redact_runtime_metadata
from neil_agent.host_runtime import (
    BENCHMARK_MINIMAL_READONLY_TOOLS,
    HostMode,
    RuntimeProfile,
    build_host_runtime,
)
from neil_agent.schemas import Message, ModelResponse, ToolCall, ToolDefinition, ToolResult
from neil_agent.subtask import SubtaskParentState, execute_readonly_subtask, subtask_parent_scope
from neil_agent.tools.registry import ToolRegistry
from neil_agent.tools.subtask import ReadonlySubtaskTools


def _settings(root: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "deepseek_api_key": "test-key",
        "workspace_root": root,
        "llm_model": "deepseek-test-model",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class SubtaskChildModel:
    def __init__(self, response: str = "subtask summary") -> None:
        self.response = response
        self.requests: list[list[Message]] = []

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
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self.requests.append(list(messages))
        yield self.response
        yield ModelResponse(content=self.response)


class ParentToolCallModel:
    def __init__(self, child_model: SubtaskChildModel) -> None:
        self.child_model = child_model
        self._stream_calls = 0

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        return "done"

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self._stream_calls += 1
        if self._stream_calls == 1:
            yield ModelResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tool-call-1",
                        name="run_readonly_subtask",
                        arguments={"prompt": "find config files"},
                    )
                ],
            )
            return
        yield "parent "
        yield "done"
        yield ModelResponse(content="parent done")


def test_readonly_subtask_profile_exposes_only_read_tools(tmp_path: Path) -> None:
    runtime = build_host_runtime(
        _settings(tmp_path),
        mode=HostMode.NONINTERACTIVE_READONLY,
        profile=RuntimeProfile.READONLY_SUBTASK,
    )
    try:
        assert runtime.profile.tool_names == BENCHMARK_MINIMAL_READONLY_TOOLS
        assert "replace_text" not in runtime.profile.tool_names
        assert "run_readonly_subtask" not in runtime.profile.tool_names
    finally:
        runtime.close()


def test_standard_cli_registers_run_readonly_subtask(tmp_path: Path) -> None:
    runtime = build_host_runtime(_settings(tmp_path), mode=HostMode.CLI)
    try:
        assert "run_readonly_subtask" in runtime.profile.tool_names
    finally:
        runtime.close()


def test_benchmark_minimal_does_not_register_run_readonly_subtask(tmp_path: Path) -> None:
    runtime = build_host_runtime(
        _settings(tmp_path),
        mode=HostMode.NONINTERACTIVE_READONLY,
        profile=RuntimeProfile.BENCHMARK_MINIMAL,
    )
    try:
        assert "run_readonly_subtask" not in runtime.profile.tool_names
    finally:
        runtime.close()


def test_execute_readonly_subtask_returns_bounded_summary(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        subtask_max_result_chars=500,
        subtask_max_tool_rounds=2,
    )
    child_model = SubtaskChildModel(response="x" * 800)
    with subtask_parent_scope(
        SubtaskParentState(
            settings=settings,
            model=child_model,
            parent_run_id="run-test-parent",
        )
    ):
        summary = execute_readonly_subtask("inspect src layout")
    assert len(summary) <= 500
    assert "truncated" in summary
    assert child_model.requests
    assert child_model.requests[0][-1].content == "inspect src layout"


def test_execute_readonly_subtask_disposes_child_runtime(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    child_model = SubtaskChildModel()
    with subtask_parent_scope(
        SubtaskParentState(
            settings=settings,
            model=child_model,
            parent_run_id="run-dispose",
        )
    ):
        execute_readonly_subtask("look around")
    follow_up = build_host_runtime(
        settings,
        mode=HostMode.NONINTERACTIVE_READONLY,
        profile=RuntimeProfile.READONLY_SUBTASK,
    )
    follow_up.close()


def test_parent_agent_keeps_only_summary_in_history(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    child_model = SubtaskChildModel(response="found two config files")
    parent_model = ParentToolCallModel(child_model)
    registry = ToolRegistry()
    ReadonlySubtaskTools().register(registry)
    agent = Agent(parent_model, registry=registry, max_tool_rounds=2)
    with subtask_parent_scope(
        SubtaskParentState(
            settings=settings,
            model=child_model,
            parent_run_id="run-parent-history",
        )
    ):
        response = "".join(agent.stream_chat("explore configs"))
    assert response == "parent done"
    roles = [message.role for message in agent.messages]
    assert roles.count("user") == 2
    assert roles.count("assistant") == 2
    tool_message = next(
        message
        for message in agent.messages
        if message.role == "user" and message.tool_results
    )
    assert "found two config files" in tool_message.tool_results[0].content
    assert "inspect src layout" not in " ".join(
        message.content for message in agent.messages if message.role == "user"
    )


def test_forwarded_subtask_events_include_parent_run_id() -> None:
    metadata = redact_runtime_metadata(
        "agent_turn",
        {"parent_run_id": "run-abc123", "input_chars": 12},
    )
    assert any(item.name == "parent_run_id" and item.value == "run-abc123" for item in metadata)


def test_execute_readonly_subtask_requires_parent_scope(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="仅在 CLI 或 Web"):
        execute_readonly_subtask("missing parent")


def test_execute_readonly_subtask_honours_cancel(tmp_path: Path) -> None:
    settings = _settings(tmp_path, subtask_timeout_seconds=30.0)

    class SlowModel(SubtaskChildModel):
        def stream(
            self,
            messages: Sequence[Message],
            *,
            system_prompt: str,
            tools: Sequence[ToolDefinition] = (),
        ) -> Iterator[str | ModelResponse]:
            cancel.set()
            yield from super().stream(messages, system_prompt=system_prompt, tools=tools)

    cancel = Event()
    cancel.set()
    with subtask_parent_scope(
        SubtaskParentState(
            settings=settings,
            model=SlowModel(),
            parent_run_id="run-cancel",
            cancel=cancel,
        )
    ):
        with pytest.raises(ToolError, match="已取消"):
            execute_readonly_subtask("slow task")


def test_subtask_runtime_events_forward_with_parent_link(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    child_model = SubtaskChildModel()
    forwarded: list[RuntimeEvent] = []

    def capture(event: RuntimeEvent) -> None:
        forwarded.append(event)

    bus = EventBus(queue_size=16, max_observers=1)
    subscription = bus.subscribe(capture)
    registry = ToolRegistry()
    ReadonlySubtaskTools().register(registry)
    parent = Agent(
        ParentToolCallModel(child_model),
        registry=registry,
        max_tool_rounds=2,
        event_bus=bus,
    )
    try:
        with subtask_parent_scope(
            SubtaskParentState(
                settings=settings,
                model=child_model,
                parent_run_id="run-forward",
                forward_runtime_event=capture,
            )
        ):
            list(parent.stream_chat("delegate"))
    finally:
        subscription.close()
        bus.close(timeout=0.25)

    child_turns = [
        event
        for event in forwarded
        if event.stage == "agent_turn"
        and any(item.name == "parent_run_id" for item in event.metadata)
    ]
    assert child_turns
    assert all(
        any(
            item.name == "parent_run_id" and item.value == "run-forward"
            for item in event.metadata
        )
        for event in child_turns
    )
