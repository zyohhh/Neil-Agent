"""Tests for the injectable command-line interface."""

from pathlib import Path
from collections.abc import Iterator, Sequence
from typing import cast
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from neil_agent import cli
from neil_agent.agent import Agent
from neil_agent.config import Settings
from neil_agent.errors import SessionError
from neil_agent.schemas import (
    ActivityEvent,
    Message,
    ModelResponse,
    ToolCall,
    ToolDefinition,
)
from neil_agent.session import SessionStore
from neil_agent.task import TaskStep, TaskTracker


class FakeLLMClient:
    def __init__(self, settings: Settings, retry_handler: object | None = None) -> None:
        self.settings = settings

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        return "saved reply"

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        yield "saved reply"
        yield ModelResponse(content="saved reply")


def test_run_uses_injected_console(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=tmp_path,
    )
    instruction_content = "PRIVATE-PROJECT-INSTRUCTION"
    (tmp_path / "AGENTS.md").write_text(instruction_content, encoding="utf-8")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli.ShellTools,
        "git_status_snapshot",
        lambda self: "## main...origin/main",
    )
    console = MagicMock(spec=Console)
    console.input.side_effect = [
        "/context",
        "/doctor",
        "/instructions",
        "/status",
        "/help",
        "/exit",
    ]

    cli.run(cast(Console, console))

    printed_text = "\n".join(
        str(call.args[0]) for call in console.print.call_args_list if call.args
    )
    assert "可用命令" in printed_text
    assert "可用工具：12 个（高风险操作需确认）" in printed_text
    assert "当前任务计划" in printed_text
    assert "上下文状态" in printed_text
    assert "Neil Agent Doctor" in printed_text
    assert "API Key：已配置（值已隐藏）" in printed_text
    assert "test-key" not in printed_text
    assert "项目指令" in printed_text
    assert "已生效" in printed_text
    assert instruction_content not in printed_text
    assert "最近质量检查" in printed_text
    assert "## main...origin/main" in printed_text
    assert "/status" in printed_text
    assert "/sessions" in printed_text
    assert "/resume <id>" in printed_text
    assert "/delete-session <id>" in printed_text
    assert "/doctor" in printed_text
    assert "/instructions" in printed_text
    assert "/compact" in printed_text
    assert "Neil Agent 已退出" in printed_text


def test_run_lists_and_restores_an_explicit_local_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=tmp_path,
    )
    store = SessionStore(tmp_path)
    handle = store.new_session()
    store.save(
        handle,
        (
            Message(role="user", content="continue the saved task"),
            Message(role="assistant", content="saved answer"),
        ),
        (TaskStep("Inspect saved state", "in_progress"),),
        None,
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli.ShellTools,
        "git_status_snapshot",
        lambda self: "## main...origin/main",
    )
    console = MagicMock(spec=Console)
    console.input.side_effect = [
        "/sessions",
        f"/resume {handle.session_id}",
        "/status",
        "/exit",
    ]

    cli.run(cast(Console, console))

    printed_text = "\n".join(
        str(call.args[0]) for call in console.print.call_args_list if call.args
    )
    assert "本地会话" in printed_text
    assert "存储占用" in printed_text
    assert handle.session_id in printed_text
    assert "continue the saved task" in printed_text
    assert "已恢复本地会话" in printed_text
    assert "Inspect saved state" in printed_text


def test_successful_chat_is_saved_automatically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=tmp_path,
    )
    instruction_content = "PRIVATE-PROJECT-INSTRUCTION"
    (tmp_path / "AGENTS.md").write_text(instruction_content, encoding="utf-8")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "LLMClient", FakeLLMClient)
    console = MagicMock(spec=Console)
    console.input.side_effect = ["remember this", "/exit"]

    cli.run(cast(Console, console))

    index = SessionStore(tmp_path).list_sessions()
    assert len(index.sessions) == 1
    snapshot = SessionStore(tmp_path).load(index.sessions[0].session_id)
    assert [message.content for message in snapshot.messages] == [
        "remember this",
        "saved reply",
    ]
    payload = (
        SessionStore(tmp_path).root / f"{index.sessions[0].session_id}.json"
    ).read_text(encoding="utf-8")
    assert instruction_content not in payload


def test_run_explicitly_compacts_and_persists_complete_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=tmp_path,
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "LLMClient", FakeLLMClient)
    console = MagicMock(spec=Console)
    console.input.side_effect = [
        "first " + "x" * 1_000,
        "second",
        "third",
        "/compact",
        "/exit",
    ]

    cli.run(cast(Console, console))

    index = SessionStore(tmp_path).list_sessions()
    snapshot = SessionStore(tmp_path).load(index.sessions[0].session_id)
    printed_text = "\n".join(
        str(call.args[0]) for call in console.print.call_args_list if call.args
    )
    assert "对话压缩完成" in printed_text
    assert len(snapshot.messages) == 6
    assert snapshot.messages[0].content.startswith("[Neil Agent /compact checkpoint]")
    assert "saved reply" in snapshot.messages[1].content
    assert [snapshot.messages[index].content for index in (2, 4)] == [
        "second",
        "third",
    ]


def test_compaction_save_failure_keeps_original_in_memory_history(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=tmp_path,
    )
    agent = Agent(FakeLLMClient(settings))
    for user_input in ("first " + "x" * 1_000, "second", "third"):
        agent.chat(user_input)
    original_messages = agent.messages
    real_store = SessionStore(tmp_path)
    session_store = MagicMock(spec=SessionStore)
    session_store.save.side_effect = SessionError("simulated save failure")
    console = MagicMock(spec=Console)
    renderer = cli.TerminalRenderer(cast(Console, console))

    cli._compact_session(
        cast(Console, console),
        renderer,
        agent,
        cast(SessionStore, session_store),
        real_store.new_session(),
        TaskTracker(),
    )

    assert agent.messages == original_messages
    printed_text = "\n".join(
        str(call.args[0]) for call in console.print.call_args_list if call.args
    )
    assert "压缩结果未保存" in printed_text
    assert "原历史未改变" in printed_text


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


@pytest.mark.parametrize(
    ("answer", "deleted"),
    [("y", True), ("", False)],
)
def test_delete_session_requires_explicit_yes(
    answer: str,
    deleted: bool,
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    handle = store.new_session()
    store.save(
        handle,
        (
            Message(role="user", content="old request"),
            Message(role="assistant", content="old answer"),
        ),
        (),
        None,
    )
    console = MagicMock(spec=Console)
    console.input.return_value = answer

    cli._delete_session(
        cast(Console, console),
        store,
        handle.session_id,
        "20990101T000000000000Z-feedface",
    )

    path = store.root / f"{handle.session_id}.json"
    assert path.exists() is not deleted
    printed_text = "\n".join(
        str(call.args[0]) for call in console.print.call_args_list if call.args
    )
    assert "old request" in printed_text
    assert ("已删除本地会话" in printed_text) is deleted


def test_delete_session_refuses_current_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    handle = store.new_session()
    console = MagicMock(spec=Console)

    cli._delete_session(
        cast(Console, console),
        store,
        handle.session_id,
        handle.session_id,
    )

    console.input.assert_not_called()
    assert "不能删除当前会话" in str(console.print.call_args.args[0])


def test_terminal_renderer_coordinates_activity_and_streamed_text() -> None:
    console = MagicMock(spec=Console)
    status = MagicMock()
    console.status.return_value = status
    renderer = cli.TerminalRenderer(cast(Console, console))

    renderer.show_activity(
        ActivityEvent(
            status="running",
            message="分析用户请求",
            details=("模型轮次：1", "上下文消息：1 条"),
        )
    )
    renderer.show_text("先读取文件")
    renderer.show_activity(
        ActivityEvent(
            status="succeeded",
            message="读取文件",
            details=("路径：README.md", "结果：20 行，500 字符", "耗时：0.1s"),
        )
    )
    renderer.show_text("最终回答")
    renderer.finish_answer()

    printed = [call.args[0] for call in console.print.call_args_list if call.args]
    assert printed == [
        "[bold green]Neil Agent[/bold green] > ",
        "先读取文件",
        "[ok] 读取文件",
        "    路径：README.md",
        "    结果：20 行，500 字符",
        "    耗时：0.1s",
        "[bold green]Neil Agent[/bold green] > ",
        "最终回答",
    ]
    status.start.assert_called_once_with()
    status.stop.assert_called_once_with()
    status_label = console.status.call_args.args[0]
    assert isinstance(status_label, cli.ActivityStatusLabel)
    assert status_label.message == "分析用户请求"
    assert status_label.detail == "模型轮次：1"
    assert str(status_label.__rich__()).startswith("分析用户请求 · 模型轮次：1 · ")
    assert sum(not call.args for call in console.print.call_args_list) == 2


def test_terminal_renderer_closes_answer_before_plan_update() -> None:
    console = MagicMock(spec=Console)
    status = MagicMock()
    console.status.return_value = status
    renderer = cli.TerminalRenderer(cast(Console, console))

    renderer.show_activity(ActivityEvent(status="running", message="创建任务计划"))
    renderer.show_text("我先制定计划")
    renderer.show_plan("[>] Inspect\n[ ] Verify")

    printed = [call.args[0] for call in console.print.call_args_list if call.args]
    assert printed[-2:] == [
        "[bold blue]任务计划已更新[/bold blue]",
        "[>] Inspect\n[ ] Verify",
    ]
    status.stop.assert_called_once_with()
    assert sum(not call.args for call in console.print.call_args_list) == 1
