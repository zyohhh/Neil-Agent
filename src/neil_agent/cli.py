"""Interactive terminal interface for Neil Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic

from pydantic import ValidationError
from rich.console import Console
from rich.status import Status
from rich.text import Text

from .agent import Agent
from .config import get_settings
from .errors import NeilAgentError, SessionError
from .llm import LLMClient
from .schemas import ActivityEvent, ToolCall
from .session import SessionHandle, SessionStore
from .task import TaskTracker
from .tools import FileSystemTools, ShellTools, ToolRegistry

EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit"}
CLEAR_COMMANDS = {"clear", "/clear"}
HELP_COMMANDS = {"help", "/help"}
STATUS_COMMANDS = {"status", "/status"}
SESSIONS_COMMANDS = {"sessions", "/sessions"}
RESUME_COMMANDS = {"resume", "/resume"}


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

    llm = LLMClient(settings)
    agent = Agent(
        llm,
        system_prompt=settings.system_prompt,
        max_rounds=settings.max_rounds,
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
    console.print("  /sessions 显示本地会话")
    console.print("  /resume <id> 恢复指定会话")
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
    if not index.sessions:
        console.print("  （尚无已保存会话）")
    for summary in index.sessions:
        marker = "*" if summary.session_id == current_session_id else " "
        updated_at = summary.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        console.print(
            f"{marker} {summary.session_id}  {updated_at}  {summary.round_count} 轮",
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
