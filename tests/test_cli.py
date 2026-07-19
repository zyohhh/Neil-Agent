"""Tests for the injectable command-line interface."""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from neil_agent import cli
from neil_agent.config import Settings
from neil_agent.schemas import ActivityEvent, ToolCall


def test_run_uses_injected_console(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=tmp_path,
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli.ShellTools,
        "git_status_snapshot",
        lambda self: "## main...origin/main",
    )
    console = MagicMock(spec=Console)
    console.input.side_effect = ["/status", "/help", "/exit"]

    cli.run(cast(Console, console))

    printed_text = "\n".join(
        str(call.args[0]) for call in console.print.call_args_list if call.args
    )
    assert "可用命令" in printed_text
    assert "可用工具：12 个（高风险操作需确认）" in printed_text
    assert "当前任务计划" in printed_text
    assert "最近质量检查" in printed_text
    assert "## main...origin/main" in printed_text
    assert "/status" in printed_text
    assert "Neil Agent 已退出" in printed_text


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("y", True), ("", False)],
)
def test_confirm_tool_call_requires_explicit_yes(answer: str, expected: bool) -> None:
    console = MagicMock(spec=Console)
    console.input.return_value = answer
    call = ToolCall(id="call-write", name="write_file", arguments={})

    approved = cli._confirm_tool_call(
        cast(Console, console),
        call,
        "--- a/file\n+++ b/file",
    )

    assert approved is expected


def test_terminal_renderer_coordinates_activity_and_streamed_text() -> None:
    console = MagicMock(spec=Console)
    renderer = cli.TerminalRenderer(cast(Console, console))

    renderer.show_activity(ActivityEvent(status="running", message="正在分析请求…"))
    renderer.show_text("先读取文件")
    renderer.show_activity(
        ActivityEvent(status="succeeded", message="读取文件完成（0.1s）")
    )
    renderer.show_text("最终回答")
    renderer.finish_answer()

    printed = [call.args[0] for call in console.print.call_args_list if call.args]
    assert printed == [
        "[>] 正在分析请求…",
        "[bold green]Neil Agent[/bold green] > ",
        "先读取文件",
        "[ok] 读取文件完成（0.1s）",
        "[bold green]Neil Agent[/bold green] > ",
        "最终回答",
    ]
    assert sum(not call.args for call in console.print.call_args_list) == 2


def test_terminal_renderer_closes_answer_before_plan_update() -> None:
    console = MagicMock(spec=Console)
    renderer = cli.TerminalRenderer(cast(Console, console))

    renderer.show_text("我先制定计划")
    renderer.show_plan("[>] Inspect\n[ ] Verify")

    printed = [call.args[0] for call in console.print.call_args_list if call.args]
    assert printed[-2:] == [
        "[bold blue]任务计划已更新[/bold blue]",
        "[>] Inspect\n[ ] Verify",
    ]
    assert sum(not call.args for call in console.print.call_args_list) == 1
