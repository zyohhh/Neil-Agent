"""Tests for the injectable command-line interface."""

from collections.abc import Iterator, Sequence
from io import StringIO
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from rich.console import Console
from rich.panel import Panel

from neil_agent import cli
from neil_agent.agent import Agent
from neil_agent.config import Settings
from neil_agent.errors import SessionError
from neil_agent.instructions import load_project_instructions
from neil_agent.schemas import (
    ActivityEvent,
    Message,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from neil_agent.session import SessionStore
from neil_agent.task import TaskStep, TaskTracker


class FakeLLMClient:
    def __init__(self, settings: Settings, retry_handler: object | None = None) -> None:
        self.settings = settings
        self.system_prompts: list[str] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        self.system_prompts.append(system_prompt)
        return "saved reply"

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self.system_prompts.append(system_prompt)
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
        "/cockpit",
        "/context",
        "/doctor",
        "/instructions",
        "/permissions",
        "/status",
        "/help",
        "/exit",
    ]

    cli.run(cast(Console, console))

    printed_text = "\n".join(
        str(call.args[0]) for call in console.print.call_args_list if call.args
    )
    panels = [
        call.args[0]
        for call in console.print.call_args_list
        if call.args and isinstance(call.args[0], Panel)
    ]
    panel_texts = [_render(panel, width=100) for panel in panels]
    welcome_texts = [text for text in panel_texts if "NEIL AGENT" in text]
    cockpit_texts = [text for text in panel_texts if "MISSION CONTROL" in text]
    assert len(welcome_texts) == 1
    assert len(cockpit_texts) == 1
    welcome_text = welcome_texts[0]
    cockpit_text = cockpit_texts[0]
    assert "NEIL AGENT" in welcome_text
    assert "deepseek-v4-flash" in welcome_text
    assert "12 个可用 · 5 个操作需要批准" in welcome_text
    assert "已加载 1 个来源" in welcome_text
    assert "CONTEXT TOMOGRAPHY" in cockpit_text
    assert "SECURITY SHIELD" in cockpit_text
    assert instruction_content not in cockpit_text
    assert "可用命令" in printed_text
    assert "当前任务计划" in printed_text
    assert "上下文状态" in printed_text
    assert "Neil Agent Doctor" in printed_text
    assert "API Key：已配置（值已隐藏）" in printed_text
    assert "test-key" not in printed_text
    assert "项目指令" in printed_text
    assert "权限与安全边界" in printed_text
    assert "OS 沙箱：当前未实现" in printed_text
    assert "已生效" in printed_text
    assert instruction_content not in printed_text
    assert "最近质量检查" in printed_text
    assert "## main...origin/main" in printed_text
    assert "/status" in printed_text
    assert "/cockpit" in printed_text
    assert "/sessions" in printed_text
    assert "/resume <id>" in printed_text
    assert "/delete-session <id>" in printed_text
    assert "/doctor" in printed_text
    assert "/instructions" in printed_text
    assert "/reload-instructions" in printed_text
    assert "/init" in printed_text
    assert "/compact" in printed_text
    assert "/rename-session <标题>" in printed_text
    assert "Neil Agent 已退出" in printed_text


def test_welcome_panel_remains_readable_in_a_narrow_terminal(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("PRIVATE-RULE", encoding="utf-8")
    panel = cli._build_welcome_panel(
        model="deepseek-v4-flash",
        thinking_enabled=True,
        workspace_root=str(tmp_path / "a-very-long-workspace-directory"),
        tool_count=12,
        approval_tool_count=5,
        session_id="20260721T120000000000Z-deadbeef",
        instructions=load_project_instructions(tmp_path),
    )

    output = _render(panel, width=52)

    assert "NEIL AGENT" in output
    assert "本地 Coding Agent 已准备就绪" in output
    assert "deepseek-v4-flash" in output
    assert "开启" in output
    assert "/help" in output
    assert "PRIVATE-RULE" not in output


@pytest.mark.parametrize(
    ("invalid", "expected"),
    [
        (False, "未配置 · 使用 /init 创建"),
        (True, "未加载 · 项目指令必须使用 UTF-8 编码"),
    ],
)
def test_welcome_panel_explains_inactive_instruction_state(
    invalid: bool,
    expected: str,
    tmp_path: Path,
) -> None:
    if invalid:
        (tmp_path / "AGENTS.md").write_bytes(b"\xff\xfe")
    panel = cli._build_welcome_panel(
        model="deepseek-v4-flash",
        thinking_enabled=False,
        workspace_root=str(tmp_path),
        tool_count=12,
        approval_tool_count=5,
        session_id="20260721T120000000000Z-deadbeef",
        instructions=load_project_instructions(tmp_path),
    )

    assert expected in _render(panel, width=100)


def _render(renderable: object, *, width: int) -> str:
    output = StringIO()
    console = Console(
        file=output,
        width=width,
        color_system=None,
        force_terminal=False,
    )
    console.print(renderable)
    return output.getvalue()


def test_main_routes_one_shot_arguments_without_starting_interactive_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=tmp_path,
    )
    calls: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    def fake_run_noninteractive(
        received_settings: Settings,
        prompt: str,
        *,
        output_format: str,
        stdout: object,
        stderr: object,
        save_session: bool,
    ) -> int:
        assert received_settings is settings
        calls.append((prompt, output_format, save_session))
        return 0

    monkeypatch.setattr(cli, "run_noninteractive", fake_run_noninteractive)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(
            [
                "--print",
                "inspect this project",
                "--output-format",
                "stream-json",
                "--save-session",
            ]
        )

    assert exit_info.value.code == 0
    assert calls == [("inspect this project", "stream-json", True)]


def test_config_error_message_does_not_echo_invalid_raw_value() -> None:
    secret_value = "not-a-valid-url-secret"
    with pytest.raises(cli.ValidationError) as error_info:
        Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            deepseek_base_url=secret_value,
        )

    message = cli._config_error_message(error_info.value)
    assert "deepseek_base_url" in message
    assert secret_value not in message


def test_context_distinguishes_estimate_from_server_usage() -> None:
    agent = Agent(FakeLLMClient.__new__(FakeLLMClient))
    agent.restore_messages(
        (
            Message(role="user", content="hello"),
            Message(role="assistant", content="done"),
        ),
        TokenUsage(input_tokens=100, output_tokens=20),
    )
    console = MagicMock(spec=Console)

    cli._show_context(cast(Console, console), agent)

    output = "\n".join(
        str(call.args[0]) for call in console.print.call_args_list if call.args
    )
    assert "最近服务端实测" in output
    assert "输入 100" in output
    assert "输出 20" in output
    assert "合计 120 token" in output
    assert "不能预测下一次费用" in output


def test_rewind_file_requires_confirmation_and_restores_latest_edit(
    tmp_path: Path,
) -> None:
    target = tmp_path / "example.txt"
    target.write_text("before", encoding="utf-8")
    tools = cli.FileSystemTools(tmp_path)
    tools.write_file("example.txt", "after")
    console = MagicMock(spec=Console)
    console.input.return_value = "yes"

    cli._rewind_latest_file(cast(Console, console), tools)

    assert target.read_text(encoding="utf-8") == "before"
    printed = "\n".join(
        str(call.args[0]) for call in console.print.call_args_list if call.args
    )
    assert "恢复最近文件编辑" in printed
    assert "已恢复文件原内容" in printed


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
    assert index.valid_count == 2
    backup = next(
        item for item in index.sessions if item.session_id != snapshot.session_id
    )
    backup_snapshot = SessionStore(tmp_path).load(backup.session_id)
    assert len(backup_snapshot.messages) == 6
    assert not backup_snapshot.messages[0].content.startswith(
        "[Neil Agent /compact checkpoint]"
    )


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


def test_reload_instructions_keeps_old_snapshot_when_new_chain_is_invalid(
    tmp_path: Path,
) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text("OLD-RULE", encoding="utf-8")
    current = load_project_instructions(tmp_path)
    model = FakeLLMClient(
        Settings(_env_file=None, deepseek_api_key="test-key", workspace_root=tmp_path)
    )
    agent = Agent(model, project_instructions=current.prompt_section())
    console = MagicMock(spec=Console)
    source.write_bytes(b"\xff\xfe")

    reloaded = cli._reload_instructions(
        cast(Console, console),
        agent,
        tmp_path,
        tmp_path,
        current,
    )
    agent.chat("verify")

    assert reloaded is None
    assert "OLD-RULE" in model.system_prompts[-1]
    printed_text = "\n".join(
        str(call.args[0]) for call in console.print.call_args_list if call.args
    )
    assert "继续使用旧的有效指令快照" in printed_text


def test_reload_instructions_replaces_prompt_without_restarting(tmp_path: Path) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text("OLD-RULE", encoding="utf-8")
    current = load_project_instructions(tmp_path)
    model = FakeLLMClient(
        Settings(_env_file=None, deepseek_api_key="test-key", workspace_root=tmp_path)
    )
    agent = Agent(model, project_instructions=current.prompt_section())
    console = MagicMock(spec=Console)
    source.write_text("NEW-RULE", encoding="utf-8")

    reloaded = cli._reload_instructions(
        cast(Console, console),
        agent,
        tmp_path,
        tmp_path,
        current,
    )
    agent.chat("verify")

    assert reloaded is not None
    assert "NEW-RULE" in model.system_prompts[-1]
    assert "OLD-RULE" not in model.system_prompts[-1]


@pytest.mark.parametrize("answer", ["", "y"])
def test_init_requires_approval_and_never_overwrites(
    answer: str,
    tmp_path: Path,
) -> None:
    current = load_project_instructions(tmp_path)
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=tmp_path,
    )
    agent = Agent(FakeLLMClient(settings))
    console = MagicMock(spec=Console)
    console.input.return_value = answer

    initialized = cli._initialize_instructions(
        cast(Console, console),
        agent,
        tmp_path,
        tmp_path,
        current,
    )

    assert (tmp_path / "AGENTS.md").exists() is (answer == "y")
    assert (initialized is not None) is (answer == "y")


def test_run_renames_and_searches_current_session(
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
        "investigate parser behavior",
        "/rename-session Parser work",
        "/sessions parser",
        "/exit",
    ]

    cli.run(cast(Console, console))

    index = SessionStore(tmp_path).list_sessions("parser")
    assert index.matched_count == 1
    assert index.sessions[0].title == "Parser work"
    printed_text = "\n".join(
        str(call.args[0]) for call in console.print.call_args_list if call.args
    )
    assert "本地会话搜索：parser" in printed_text
    assert "Parser work" in printed_text


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
