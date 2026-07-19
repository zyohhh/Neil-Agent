"""Tests for visible task planning and status snapshots."""

from __future__ import annotations

import pytest

from neil_agent.errors import ToolError
from neil_agent.schemas import ToolCall, ToolResult
from neil_agent.task import TaskTracker
from neil_agent.tools.registry import ToolRegistry


def test_set_task_plan_starts_first_step_and_notifies_ui() -> None:
    updates: list[str] = []
    tracker = TaskTracker(change_handler=updates.append)
    registry = ToolRegistry()
    tracker.register(registry)

    result = registry.execute(
        ToolCall(
            id="call-plan",
            name="set_task_plan",
            arguments={"steps": ["Inspect code", "Implement change", "Run tests"]},
        )
    )

    assert result.is_error is False
    assert [step.status for step in tracker.steps] == [
        "in_progress",
        "pending",
        "pending",
    ]
    assert "[>] 1. Inspect code [in_progress]" in result.content
    assert updates == [tracker.format_plan()]
    assert [definition.name for definition in registry.definitions] == [
        "set_task_plan",
        "update_task_step",
    ]


@pytest.mark.parametrize(
    "steps",
    [
        ["one", "two", "three", "four", "five", "six"],
        ["duplicate", "duplicate"],
        ["valid", "   "],
    ],
)
def test_set_task_plan_rejects_invalid_steps(steps: list[str]) -> None:
    tracker = TaskTracker()

    with pytest.raises(ToolError):
        tracker.set_task_plan(steps)

    assert tracker.steps == ()


def test_update_task_step_advances_plan_in_order() -> None:
    tracker = TaskTracker()
    tracker.set_task_plan(["Inspect", "Implement", "Verify"])

    first = tracker.update_task_step(1, "completed")
    with pytest.raises(ToolError, match="当前进行中"):
        tracker.update_task_step(3, "in_progress")
    second = tracker.update_task_step(2, "completed")
    final = tracker.update_task_step(3, "completed")

    assert "[x] 1. Inspect [completed]" in first
    assert "[>] 2. Implement [in_progress]" in first
    assert "[>] 3. Verify [in_progress]" in second
    assert all(step.status == "completed" for step in tracker.steps)
    assert "[x] 3. Verify [completed]" in final


def test_record_quality_check_distinguishes_passed_failed_and_not_run() -> None:
    tracker = TaskTracker()
    call = ToolCall(
        id="call-quality",
        name="run_quality_check",
        arguments={"check": "pytest"},
    )

    tracker.record_tool_result(
        call,
        ToolResult(
            tool_call_id=call.id,
            content=(
                "Command: python -m pytest -q\n"
                "Working directory: D:/project\n"
                "Exit code: 0\n"
                "Output:\n53 passed"
            ),
        ),
    )
    passed = tracker.latest_quality_check
    assert passed is not None
    assert passed.status == "passed"
    assert passed.command == "python -m pytest -q"
    assert passed.exit_code == 0
    assert passed.output == "53 passed"

    tracker.record_tool_result(
        call,
        ToolResult(
            tool_call_id=call.id,
            content="用户拒绝执行工具：run_quality_check",
            is_error=True,
        ),
    )
    not_run = tracker.latest_quality_check
    assert not_run is not None
    assert not_run.status == "not_run"
    assert not_run.command is None

    tracker.record_tool_result(
        call,
        ToolResult(
            tool_call_id=call.id,
            content="找不到命令：python",
            is_error=True,
        ),
    )
    startup_failed = tracker.latest_quality_check
    assert startup_failed is not None
    assert startup_failed.status == "failed"

    tracker.record_tool_result(
        call,
        ToolResult(
            tool_call_id=call.id,
            content="Command: python -m pytest -q\nExit code: 1\nOutput:\n1 failed",
            is_error=True,
        ),
    )
    failed = tracker.latest_quality_check
    assert failed is not None
    assert failed.status == "failed"
    assert failed.exit_code == 1


def test_format_status_and_clear_cover_all_session_state() -> None:
    tracker = TaskTracker()
    tracker.set_task_plan(["Inspect", "Verify"])
    call = ToolCall(
        id="call-quality",
        name="run_quality_check",
        arguments={"check": "ruff"},
    )
    tracker.record_tool_result(
        call,
        ToolResult(
            tool_call_id=call.id,
            content="Command: python -m ruff check .\nExit code: 0\nOutput:\nAll checks passed!",
        ),
    )

    status = tracker.format_status("## main...origin/main\n M app.py")
    tracker.clear()

    assert "当前任务计划" in status
    assert "ruff: passed" in status
    assert "All checks passed!" in status
    assert "本地 Git 状态" in status
    assert "M app.py" in status
    assert tracker.steps == ()
    assert tracker.latest_quality_check is None
