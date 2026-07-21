"""Interactive terminal interface for Neil Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from pydantic import ValidationError
from rich.console import Console
from rich.status import Status
from rich.text import Text

from .agent import Agent
from .config import Settings, get_settings
from .diagnostics import run_diagnostics
from .errors import NeilAgentError, SessionError
from .instructions import ProjectInstructions, load_project_instructions
from .llm import LLMClient
from .schemas import ActivityEvent, ToolCall
from .session import SessionHandle, SessionStore
from .task import TaskTracker
from .tools import FileSystemTools, ShellTools, ToolRegistry

EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit"}
CLEAR_COMMANDS = {"clear", "/clear"}
HELP_COMMANDS = {"help", "/help"}
STATUS_COMMANDS = {"status", "/status"}
CONTEXT_COMMANDS = {"context", "/context"}
DOCTOR_COMMANDS = {"doctor", "/doctor"}
INSTRUCTIONS_COMMANDS = {"instructions", "/instructions"}
COMPACT_COMMANDS = {"compact", "/compact"}
SESSIONS_COMMANDS = {"sessions", "/sessions"}
RESUME_COMMANDS = {"resume", "/resume"}
DELETE_SESSION_COMMANDS = {"delete-session", "/delete-session"}


@dataclass(slots=True)
class ActivityStatusLabel:
    """Render a live activity label with continuously updated elapsed time."""

    message: str
    detail: str | None = None
    started_at: float = field(default_factory=monotonic)

    def __rich__(self) -> Text:
        label = Text(self.message)
        if self.detail is not None:
            label.append(f" · {self.detail}", style="dim")
        label.append(f" · {monotonic() - self.started_at:.1f}s", style="dim")
        return label


class TerminalRenderer:
    """Coordinate activity, plan, and streamed answer output."""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._answer_active = False
        self._line_open = False
        self._status: Status | None = None

    def show_activity(self, event: ActivityEvent) -> None:
        """Animate running work and persist completed activity with details."""

        self._stop_status()
        self.ensure_line_closed()
        if event.status == "running":
            label = ActivityStatusLabel(
                event.message,
                event.details[0] if event.details else None,
            )
            self._status = self._console.status(
                label,
                spinner="dots",
                spinner_style="cyan",
            )
            self._status.start()
            return

        marker, style = {
            "waiting": ("[?]", "yellow"),
            "succeeded": ("[ok]", "green"),
            "skipped": ("[-]", "yellow"),
            "failed": ("[!]", "red"),
        }[event.status]
        self._console.print(
            f"{marker} {event.message}",
            style=style,
            markup=False,
            highlight=False,
        )
        for detail in event.details:
            self._console.print(
                f"    {detail}",
                style="dim",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )

    def show_text(self, chunk: str) -> None:
        """Stream assistant text, reopening its prefix after activity output."""

        self._stop_status()
        if not self._answer_active:
            self._console.print("[bold green]Neil Agent[/bold green] > ", end="")
            self._answer_active = True
        self._console.print(
            chunk,
            end="",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        self._line_open = not chunk.endswith(("\n", "\r"))

    def show_plan(self, plan: str) -> None:
        """Display a plan without corrupting an active answer line."""

        self._stop_status()
        self.ensure_line_closed()
        self._console.print("[bold blue]任务计划已更新[/bold blue]")
        self._console.print(plan, markup=False, highlight=False, soft_wrap=True)

    def ensure_line_closed(self) -> None:
        """Close the current answer segment before non-answer output."""

        if self._answer_active and self._line_open:
            self._console.print()
        self._answer_active = False
        self._line_open = False

    def finish_answer(self) -> None:
        """Finish the current answer segment if one was started."""

        self._stop_status()
        self.ensure_line_closed()

    def _stop_status(self) -> None:
        if self._status is None:
            return
        self._status.stop()
        self._status = None


def main() -> None:
    """Create the real terminal console and run the application."""

    run(Console())


def run(console: Console) -> None:
    """Run the interactive chat loop using an injected console."""

    try:
        settings = get_settings()
    except ValidationError as error:
        _show_config_error(console, error)
        raise SystemExit(1) from None

    registry = ToolRegistry()
    try:
        filesystem_tools = FileSystemTools(settings.workspace_root)
    except ValueError as error:
        console.print(f"[bold red]工作区配置错误：[/bold red]{error}")
        raise SystemExit(1) from None
    filesystem_tools.register(registry)
    project_instructions = load_project_instructions(filesystem_tools.root)
    session_store = SessionStore(filesystem_tools.root)
    current_session = session_store.new_session()
    shell_tools = ShellTools(
        settings.workspace_root,
        timeout=settings.command_timeout,
        max_output_chars=settings.max_command_output_chars,
    )
    shell_tools.register(registry)
    renderer = TerminalRenderer(console)
    task_tracker = TaskTracker(change_handler=renderer.show_plan)
    task_tracker.register(registry)

    llm = LLMClient(settings, retry_handler=renderer.show_activity)
    agent = Agent(
        llm,
        system_prompt=settings.system_prompt,
        project_instructions=project_instructions.prompt_section(),
        max_rounds=settings.max_rounds,
        max_context_chars=settings.max_context_chars,
        registry=registry,
        max_tool_rounds=settings.max_tool_rounds,
        approval_handler=lambda call, preview: _confirm_tool_call(
            console,
            call,
            preview,
        ),
        task_tracker=task_tracker,
        activity_handler=renderer.show_activity,
    )
    _show_welcome(
        console,
        settings.deepseek_model,
        settings.thinking_enabled,
        str(filesystem_tools.root),
        len(registry.definitions),
        current_session.session_id,
    )
    _show_instruction_startup(console, project_instructions)

    while True:
        try:
            user_input = console.input("\n[bold cyan]你[/bold cyan] > ").strip()
        except (EOFError, KeyboardInterrupt):
            _show_goodbye(console)
            return

        command_name, _, command_argument = user_input.partition(" ")
        command = command_name.lower()
        if command in EXIT_COMMANDS and not command_argument:
            _show_goodbye(console)
            return
        if command in CLEAR_COMMANDS and not command_argument:
            agent.clear()
            current_session = session_store.new_session()
            console.print(
                f"[dim]已开始新的本地会话：{current_session.session_id}[/dim]"
            )
            continue
        if command in HELP_COMMANDS and not command_argument:
            _show_help(console)
            continue
        if command in STATUS_COMMANDS and not command_argument:
            _show_status(console, task_tracker, shell_tools, current_session)
            continue
        if command in CONTEXT_COMMANDS:
            if command_argument:
                console.print("[yellow]用法：/context[/yellow]")
            else:
                _show_context(console, agent)
            continue
        if command in DOCTOR_COMMANDS:
            if command_argument:
                console.print("[yellow]用法：/doctor[/yellow]")
            else:
                _show_doctor(
                    console,
                    settings,
                    filesystem_tools.root,
                    session_store,
                    shell_tools,
                )
            continue
        if command in INSTRUCTIONS_COMMANDS:
            if command_argument:
                console.print("[yellow]用法：/instructions[/yellow]")
            else:
                _show_instructions(console, project_instructions)
            continue
        if command in COMPACT_COMMANDS:
            if command_argument:
                console.print("[yellow]用法：/compact[/yellow]")
            else:
                _compact_session(
                    console,
                    renderer,
                    agent,
                    session_store,
                    current_session,
                    task_tracker,
                )
            continue
        if command in SESSIONS_COMMANDS and not command_argument:
            _show_sessions(console, session_store, current_session.session_id)
            continue
        if command in RESUME_COMMANDS:
            restored_session = _resume_session(
                console,
                session_store,
                command_argument.strip(),
                agent,
                task_tracker,
            )
            if restored_session is not None:
                current_session = restored_session
            continue
        if command in DELETE_SESSION_COMMANDS:
            _delete_session(
                console,
                session_store,
                command_argument.strip(),
                current_session.session_id,
            )
            continue
        if not user_input:
            continue

        response_stream = agent.stream_chat(user_input)
        try:
            for chunk in response_stream:
                renderer.show_text(chunk)
        except KeyboardInterrupt:
            response_stream.close()
            renderer.finish_answer()
            console.print("[yellow]已取消本次回答。[/yellow]")
        except NeilAgentError as error:
            renderer.finish_answer()
            console.print(f"[bold red]请求失败：[/bold red]{error}")
        else:
            renderer.finish_answer()
            try:
                session_store.save(
                    current_session,
                    agent.messages,
                    task_tracker.steps,
                    task_tracker.latest_quality_check,
                )
            except SessionError as error:
                console.print(f"[yellow]本地会话未保存：[/yellow]{error}")


def _show_welcome(
    console: Console,
    model: str,
    thinking_enabled: bool,
    workspace_root: str,
    tool_count: int,
    session_id: str,
) -> None:
    console.print("[bold green]Neil Agent[/bold green] 已启动")
    console.print(f"[dim]模型：{model}[/dim]")
    thinking_status = "开启" if thinking_enabled else "关闭"
    console.print(f"[dim]思考模式：{thinking_status}[/dim]")
    console.print(f"[dim]工作区：{workspace_root}[/dim]")
    console.print(f"[dim]当前会话：{session_id}[/dim]")
    console.print(f"[dim]可用工具：{tool_count} 个（高风险操作需确认）[/dim]")
    console.print("[dim]输入 /help 查看命令。[/dim]")


def _show_help(console: Console) -> None:
    console.print("[bold]可用命令[/bold]")
    console.print("  /clear  清空对话历史")
    console.print("  /exit   退出程序")
    console.print("  /help   显示帮助")
    console.print("  /context 显示上下文预算和当前占用")
    console.print("  /doctor 检查本地配置和运行环境")
    console.print("  /instructions 显示项目指令加载状态")
    console.print("  /compact 压缩较早的完整对话轮次")
    console.print("  /sessions 显示本地会话")
    console.print("  /resume <id> 恢复指定会话")
    console.print("  /delete-session <id> 确认后删除指定会话")
    console.print("  /status 显示任务、检查和 Git 状态")


def _show_status(
    console: Console,
    task_tracker: TaskTracker,
    shell_tools: ShellTools,
    current_session: SessionHandle,
) -> None:
    """Display current in-memory task state and a fresh local Git snapshot."""

    try:
        git_status = shell_tools.git_status_snapshot()
    except NeilAgentError as error:
        git_status = f"不可用：{error}"
    status = (
        f"当前会话\n  {current_session.session_id}\n\n"
        f"{task_tracker.format_status(git_status)}"
    )
    console.print(
        status,
        markup=False,
        highlight=False,
        soft_wrap=True,
    )


def _show_sessions(
    console: Console,
    session_store: SessionStore,
    current_session_id: str,
) -> None:
    """List newest valid local sessions without calling the model."""

    try:
        index = session_store.list_sessions()
    except SessionError as error:
        console.print(f"[bold red]无法读取本地会话：[/bold red]{error}")
        return

    console.print("[bold]本地会话[/bold]")
    console.print(
        f"  存储占用：{_format_bytes(index.total_size_bytes)}；"
        f"有效会话：{index.valid_count} 个",
        markup=False,
        highlight=False,
    )
    if not index.sessions:
        console.print("  （尚无已保存会话）")
    for summary in index.sessions:
        marker = "*" if summary.session_id == current_session_id else " "
        updated_at = summary.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        console.print(
            f"{marker} {summary.session_id}  {updated_at}  "
            f"{summary.round_count} 轮  {_format_bytes(summary.size_bytes)}",
            markup=False,
            highlight=False,
        )
        console.print(
            f"    {summary.preview}",
            style="dim",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    if index.invalid_count:
        console.print(
            f"[yellow]已跳过 {index.invalid_count} 个损坏或不兼容的会话文件。[/yellow]"
        )


def _show_context(console: Console, agent: Agent) -> None:
    """Display approximate request usage without calling the model."""

    stats = agent.context_stats()
    selected_total = stats.fixed_chars + stats.selected_message_chars
    console.print("[bold]上下文状态[/bold]")
    console.print(
        f"  预算：{stats.budget_chars:,} 字符\n"
        f"  固定开销：{stats.fixed_chars:,} 字符（系统提示词和工具定义）\n"
        f"  已保存历史：{stats.stored_rounds} 轮 / {stats.stored_messages} 条消息 / "
        f"约 {stats.stored_message_chars:,} 字符\n"
        f"  下次请求可带历史：{stats.selected_rounds} 轮 / "
        f"{stats.selected_messages} 条消息 / 约 {stats.selected_message_chars:,} 字符\n"
        f"  下次请求基础占用：约 {selected_total:,} 字符",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    if stats.omitted_rounds:
        console.print(
            f"[yellow]  已从下次请求省略 {stats.omitted_rounds} 轮旧历史。[/yellow]"
        )
    console.print(
        "[dim]字符数按 API JSON 近似计算；下一条用户输入也会占用预算。"
        "当前请求及其工具链不会被截断。[/dim]"
    )


def _show_instruction_startup(
    console: Console,
    instructions: ProjectInstructions,
) -> None:
    if instructions.active:
        console.print(
            f"[dim]项目指令：已加载 AGENTS.md（{instructions.size_bytes} 字节）[/dim]"
        )
    elif instructions.status == "invalid":
        console.print(f"[yellow]项目指令未加载：{instructions.reason}[/yellow]")


def _show_instructions(
    console: Console,
    instructions: ProjectInstructions,
) -> None:
    """Show instruction metadata without printing its content."""

    status_text = {
        "active": "已生效",
        "missing": "未找到",
        "empty": "文件为空",
        "invalid": "未加载",
    }[instructions.status]
    console.print("[bold]项目指令[/bold]")
    console.print(
        f"  状态：{status_text}\n"
        f"  来源：{instructions.source}\n"
        f"  大小：{instructions.size_bytes} 字节 / {instructions.char_count} 字符",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    if instructions.reason:
        console.print(f"[yellow]  原因：{instructions.reason}[/yellow]")
    if instructions.active:
        console.print(
            "[dim]内容已加入本进程的模型系统上下文；不会显示在此处，也不会写入会话快照。[/dim]"
        )
    else:
        console.print("[dim]当前没有项目指令加入模型上下文。[/dim]")


def _compact_session(
    console: Console,
    renderer: TerminalRenderer,
    agent: Agent,
    session_store: SessionStore,
    current_session: SessionHandle,
    task_tracker: TaskTracker,
) -> None:
    """Prepare, persist, then atomically apply an explicit history compaction."""

    renderer.show_activity(
        ActivityEvent(
            status="running",
            message="压缩较早的对话历史",
            details=("保留最近 2 个完整轮次",),
        )
    )
    try:
        prepared = agent.prepare_compaction()
    except KeyboardInterrupt:
        renderer.show_activity(
            ActivityEvent(status="skipped", message="已取消对话压缩")
        )
        return
    except NeilAgentError as error:
        renderer.show_activity(
            ActivityEvent(
                status="failed",
                message="对话压缩失败",
                details=(f"原因：{error}", "原历史未改变"),
            )
        )
        return

    try:
        session_store.save(
            current_session,
            prepared.compacted_messages,
            task_tracker.steps,
            task_tracker.latest_quality_check,
        )
    except SessionError as error:
        renderer.show_activity(
            ActivityEvent(
                status="failed",
                message="压缩结果未保存",
                details=(f"原因：{error}", "原历史未改变"),
            )
        )
        return

    agent.apply_compaction(prepared)
    saved_chars = max(prepared.old_message_chars - prepared.new_message_chars, 0)
    renderer.show_activity(
        ActivityEvent(
            status="succeeded",
            message="对话压缩完成",
            details=(
                f"已总结：{prepared.summarized_rounds} 轮",
                f"完整保留：{prepared.kept_rounds} 轮",
                f"摘要请求：{prepared.model_requests} 次",
                f"历史减少：约 {saved_chars:,} 字符",
            ),
        )
    )


def _show_doctor(
    console: Console,
    settings: Settings,
    workspace_root: Path,
    session_store: SessionStore,
    shell_tools: ShellTools,
) -> None:
    """Display read-only local diagnostics without calling DeepSeek."""

    report = run_diagnostics(
        settings,
        workspace_root,
        session_store,
        shell_tools,
    )
    console.print("[bold]Neil Agent Doctor[/bold]")
    marker_styles = {
        "ok": ("[ok]", "green"),
        "warning": ("[!]", "yellow"),
        "error": ("[x]", "red"),
    }
    for check in report.checks:
        marker, style = marker_styles[check.status]
        console.print(
            f"{marker} {check.name}：{check.summary}",
            style=style,
            markup=False,
            highlight=False,
        )
        for detail in check.details:
            console.print(
                f"    {detail}",
                style="dim",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
    if report.error_count:
        conclusion = (
            f"诊断完成：{report.error_count} 个错误，{report.warning_count} 个警告。"
        )
        style = "red"
    elif report.warning_count:
        conclusion = f"诊断完成：无错误，{report.warning_count} 个警告。"
        style = "yellow"
    else:
        conclusion = "诊断完成：本地检查全部通过。"
        style = "green"
    console.print(conclusion, style=style)
    console.print("[dim]未发送 API 请求；API Key 值未显示。[/dim]")


def _delete_session(
    console: Console,
    session_store: SessionStore,
    session_id: str,
    current_session_id: str,
) -> None:
    """Preview and explicitly confirm deletion of one inactive session."""

    if not session_id:
        console.print("[yellow]用法：/delete-session <session-id>[/yellow]")
        return
    if session_id == current_session_id:
        console.print(
            "[yellow]不能删除当前会话；请先使用 /clear 或 /resume 切换会话。[/yellow]"
        )
        return
    try:
        summary = session_store.get_summary(session_id)
    except SessionError as error:
        console.print(f"[bold red]无法删除本地会话：[/bold red]{error}")
        return

    updated_at = summary.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    console.print("\n[bold yellow]删除本地会话[/bold yellow]")
    console.print(
        f"  ID：{summary.session_id}\n"
        f"  更新时间：{updated_at}\n"
        f"  内容：{summary.round_count} 轮，{_format_bytes(summary.size_bytes)}\n"
        f"  最近请求：{summary.preview}",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    try:
        answer = console.input("[bold yellow]永久删除？[y/N][/bold yellow] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() not in {"y", "yes"}:
        console.print("[yellow]已取消删除。[/yellow]")
        return
    try:
        session_store.delete(session_id)
    except SessionError as error:
        console.print(f"[bold red]删除本地会话失败：[/bold red]{error}")
        return
    console.print(f"[green]已删除本地会话：{session_id}[/green]")


def _format_bytes(size_bytes: int) -> str:
    """Format a small local-storage value with binary units."""

    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _resume_session(
    console: Console,
    session_store: SessionStore,
    session_id: str,
    agent: Agent,
    task_tracker: TaskTracker,
) -> SessionHandle | None:
    """Restore one exact session ID into the active in-memory state."""

    if not session_id:
        console.print("[yellow]用法：/resume <session-id>[/yellow]")
        return None
    try:
        snapshot = session_store.load(session_id)
        agent.restore_messages(snapshot.messages)
        task_tracker.restore(
            snapshot.restored_steps(),
            snapshot.restored_quality_check(),
        )
    except (SessionError, ValueError) as error:
        console.print(f"[bold red]恢复会话失败：[/bold red]{error}")
        return None

    console.print(
        f"[green]已恢复本地会话：{snapshot.session_id}"
        f"（{len(snapshot.messages)} 条消息）[/green]"
    )
    console.print(
        task_tracker.format_plan(),
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    return session_store.handle_for(snapshot)


def _show_goodbye(console: Console) -> None:
    console.print("\n[dim]Neil Agent 已退出。[/dim]")


def _confirm_tool_call(console: Console, call: ToolCall, preview: str) -> bool:
    """Show an operation preview and require an explicit yes response."""

    console.print(f"\n[bold yellow]工具请求确认：{call.name}[/bold yellow]")
    console.print(preview, markup=False, highlight=False, soft_wrap=True)
    try:
        answer = console.input("[bold yellow]允许执行？[y/N][/bold yellow] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""

    approved = answer.strip().lower() in {"y", "yes"}
    if approved:
        console.print("[green]已批准。[/green]")
    else:
        console.print("[yellow]已拒绝。[/yellow]")
    return approved


def _show_config_error(console: Console, error: ValidationError) -> None:
    missing_api_key = any(
        item["type"] == "missing" and item["loc"] == ("deepseek_api_key",)
        for item in error.errors()
    )
    if missing_api_key:
        console.print("[bold red]配置错误：[/bold red]未找到 DEEPSEEK_API_KEY。")
        console.print("请复制 .env.example 为 .env，并填写你的 DeepSeek API Key。")
        return
    console.print(f"[bold red]配置错误：[/bold red]{error}")
