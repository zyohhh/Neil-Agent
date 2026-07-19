"""Tests for conversation history and streaming behavior."""

from collections.abc import Iterator, Sequence

import pytest

from neil_agent.agent import Agent
from neil_agent.errors import AgentError, LLMError
from neil_agent.schemas import (
    ActivityEvent,
    Message,
    ModelResponse,
    ToolCall,
    ToolDefinition,
)
from neil_agent.task import TaskTracker
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
    activities: list[ActivityEvent] = []
    agent = Agent(model, activity_handler=activities.append)

    with pytest.raises(LLMError, match="request failed"):
        list(agent.stream_chat("hello"))

    assert agent.messages == ()
    assert [event.status for event in activities] == ["running", "failed"]
    assert activities[-1].message == "模型请求失败"


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
    tracker = TaskTracker()
    tracker.set_task_plan(["Inspect", "Verify"])
    agent = Agent(FakeChatModel(), task_tracker=tracker)
    agent.chat("hello")

    agent.clear()

    assert agent.messages == ()
    assert tracker.steps == ()


def test_agent_passes_custom_system_prompt_to_model() -> None:
    model = FakeChatModel()
    agent = Agent(model, system_prompt="You are a Python tutor.")

    agent.chat("hello")

    assert model.system_prompts == ["You are a Python tutor."]


def test_agent_adds_quality_workflow_when_write_and_check_tools_exist() -> None:
    model = FakeChatModel()
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="write_file",
            description="Write a file.",
            input_schema={"type": "object"},
        ),
        lambda: "written",
    )
    registry.register(
        ToolDefinition(
            name="run_quality_check",
            description="Run a check.",
            input_schema={"type": "object"},
        ),
        lambda: "checked",
    )
    tracker = TaskTracker()
    tracker.register(registry)
    agent = Agent(
        model,
        system_prompt="Custom role.",
        registry=registry,
        task_tracker=tracker,
    )

    agent.chat("hello")

    prompt = model.system_prompts[0]
    assert prompt.startswith("Custom role.")
    assert "After a successful write_file or replace_text" in prompt
    assert "Command" in prompt
    assert "Exit code" in prompt
    assert "set_task_plan" in prompt
    assert "update_task_step" in prompt


def test_agent_records_quality_result_in_task_tracker() -> None:
    call = ToolCall(
        id="call-quality",
        name="run_quality_check",
        arguments={"check": "pytest"},
    )
    model = FakeChatModel()
    model.stream_responses = [
        [ModelResponse(tool_calls=(call,))],
        ["done", ModelResponse(content="done")],
    ]
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="run_quality_check",
            description="Run tests.",
            input_schema={"type": "object"},
        ),
        lambda check: "Command: python -m pytest -q\nExit code: 0\nOutput:\n2 passed",
    )
    tracker = TaskTracker()
    activities: list[ActivityEvent] = []
    agent = Agent(
        model,
        registry=registry,
        task_tracker=tracker,
        activity_handler=activities.append,
    )

    chunks = list(agent.stream_chat("run tests"))

    assert chunks == ["done"]
    record = tracker.latest_quality_check
    assert record is not None
    assert record.check == "pytest"
    assert record.status == "passed"
    assert record.output == "2 passed"
    quality_event = next(
        event
        for event in activities
        if event.status == "succeeded" and event.message == "运行质量检查"
    )
    assert "命令：python -m pytest -q" in quality_event.details
    assert "退出码：0" in quality_event.details
    assert "结果摘要：2 passed" in quality_event.details


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


def test_agent_reports_model_and_tool_activity_in_order() -> None:
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
    activities: list[ActivityEvent] = []
    agent = Agent(
        model,
        registry=registry,
        activity_handler=activities.append,
    )

    assert list(agent.stream_chat("read the README")) == ["done"]

    assert [event.status for event in activities] == [
        "running",
        "succeeded",
        "running",
        "succeeded",
        "running",
    ]
    assert activities[0].message == "分析用户请求"
    assert activities[0].details == (
        "模型轮次：1",
        "上下文消息：1 条",
        "可用工具：1 个",
    )
    assert activities[1].message == "模型请求 1 个工具"
    assert activities[1].details == ("1. read_file",)
    assert activities[2].message == "读取文件"
    assert activities[2].details == ("路径：README.md",)
    assert activities[3].message == "读取文件"
    assert "路径：README.md" in activities[3].details
    assert "结果：1 行，21 字符" in activities[3].details
    assert activities[4].message == "根据工具结果继续处理"


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

    with pytest.raises(AgentError, match="超过 1 轮"):
        list(agent.stream_chat("keep going"))

    assert agent.messages == ()


def test_agent_previews_and_executes_approved_write_tool() -> None:
    call = ToolCall(
        id="call-write",
        name="write_file",
        arguments={"path": "notes.txt", "content": "new"},
    )
    model = FakeChatModel()
    model.stream_responses = [
        [ModelResponse(tool_calls=(call,))],
        ["saved", ModelResponse(content="saved")],
    ]
    registry = ToolRegistry()
    writes: list[str] = []
    registry.register(
        ToolDefinition(
            name="write_file",
            description="Write a file.",
            input_schema={"type": "object"},
        ),
        lambda path, content: writes.append(f"{path}:{content}") or "written",
        requires_approval=True,
        preview_handler=lambda path, content: f"preview {path}:{content}",
    )
    previews: list[str] = []
    activities: list[ActivityEvent] = []
    agent = Agent(
        model,
        registry=registry,
        approval_handler=lambda tool_call, preview: previews.append(preview) or True,
        activity_handler=activities.append,
    )

    chunks = list(agent.stream_chat("update notes"))

    assert chunks == ["saved"]
    assert previews == ["preview notes.txt:new"]
    assert writes == ["notes.txt:new"]
    assert model.requests[1][-1].tool_results[0].is_error is False
    assert "run_quality_check" in model.requests[1][-1].tool_results[0].content
    write_events = [
        event
        for event in activities
        if event.message in {"写入文件", "等待批准：写入文件", "执行：写入文件"}
    ]
    assert [event.status for event in write_events] == [
        "running",
        "waiting",
        "running",
        "succeeded",
    ]
    assert write_events[0].details == (
        "路径：notes.txt",
        "内容规模：1 行，3 字符",
    )


def test_agent_returns_denied_write_to_model_without_execution() -> None:
    call = ToolCall(
        id="call-write",
        name="write_file",
        arguments={"path": "notes.txt", "content": "TOP-SECRET-CONTENT"},
    )
    model = FakeChatModel()
    model.stream_responses = [
        [ModelResponse(tool_calls=(call,))],
        ["cancelled", ModelResponse(content="cancelled")],
    ]
    registry = ToolRegistry()
    writes: list[str] = []
    registry.register(
        ToolDefinition(
            name="write_file",
            description="Write a file.",
            input_schema={"type": "object"},
        ),
        lambda path, content: writes.append(content) or "written",
        requires_approval=True,
        preview_handler=lambda path, content: f"preview {path}:{content}",
    )
    activities: list[ActivityEvent] = []
    agent = Agent(
        model,
        registry=registry,
        approval_handler=lambda tool_call, preview: False,
        activity_handler=activities.append,
    )

    chunks = list(agent.stream_chat("update notes"))

    assert chunks == ["cancelled"]
    assert writes == []
    denied_result = model.requests[1][-1].tool_results[0]
    assert denied_result.is_error is True
    assert "用户拒绝" in denied_result.content
    assert any(event.status == "skipped" for event in activities)
    activity_text = " ".join(
        (event.message + " " + " ".join(event.details)) for event in activities
    )
    assert "TOP-SECRET-CONTENT" not in activity_text
    assert "内容规模：1 行，18 字符" in activity_text
