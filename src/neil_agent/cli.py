"""Interactive terminal interface for Neil Agent."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import shlex
from typing import cast
from time import monotonic

from pydantic import ValidationError
from rich.console import Console, Group
from rich.panel import Panel
from rich.status import Status
from rich.table import Table
from rich.text import Text

from .agent import Agent
from .config import Settings, get_settings
from .diagnostics import run_diagnostics
from .errors import NeilAgentError, SessionError
from .instructions import (
    ProjectInstructionManager,
    ProjectInstructions,
    apply_project_instructions_init,
    load_project_instructions,
    prepare_project_instructions_init,
)
from .llm import LLMClient
from .schemas import ActivityEvent, ToolCall
from .session import (
    SessionHandle,
    SessionOrder,
    SessionSort,
    SessionStateFilter,
    SessionStore,
    normalize_session_title,
)
from .task import TaskTracker
from .tools import FileSystemTools, ShellTools, ToolRegistry

EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit"}
CLEAR_COMMANDS = {"clear", "/clear"}
HELP_COMMANDS = {"help", "/help"}
STATUS_COMMANDS = {"status", "/status"}
CONTEXT_COMMANDS = {"context", "/context"}
DOCTOR_COMMANDS = {"doctor", "/doctor"}
INSTRUCTIONS_COMMANDS = {"instructions", "/instructions"}
RELOAD_INSTRUCTIONS_COMMANDS = {"reload-instructions", "/reload-instructions"}
INIT_COMMANDS = {"init", "/init"}
COMPACT_COMMANDS = {"compact", "/compact"}
SESSIONS_COMMANDS = {"sessions", "/sessions"}
RESUME_COMMANDS = {"resume", "/resume"}
DELETE_SESSION_COMMANDS = {"delete-session", "/delete-session"}
RENAME_SESSION_COMMANDS = {"rename-session", "/rename-session"}
EXPORT_SESSION_COMMANDS = {"export", "/export"}
IMPORT_SESSION_COMMANDS = {"import", "/import"}


@dataclass(frozen=True, slots=True)
class SessionListOptions:
    query: str = ""
    page: int = 1
    page_size: int = 10
    sort_by: SessionSort = "updated"
    order: SessionOrder = "desc"
    state: SessionStateFilter = "all"


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
    instruction_target = _instruction_target(filesystem_tools.root)
    instruction_manager = ProjectInstructionManager(
        filesystem_tools.root,
        instruction_target,
    )
    project_instructions = instruction_manager.current
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
        max_context_tokens=settings.max_context_tokens,
        registry=registry,
        max_tool_rounds=settings.max_tool_rounds,
        approval_handler=lambda call, preview: _confirm_tool_call(
            console,
            call,
            preview,
        ),
        task_tracker=task_tracker,
        activity_handler=renderer.show_activity,
        instruction_scope_handler=instruction_manager.resolve_tool_call,
    )
    _show_welcome(
        console,
        settings.deepseek_model,
        settings.thinking_enabled,
        str(filesystem_tools.root),
        len(registry.definitions),
        sum(
            registry.requires_approval(definition.name)
            for definition in registry.definitions
        ),
        current_session.session_id,
        project_instructions,
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
                _show_instructions(console, instruction_manager.current)
            continue
        if command in RELOAD_INSTRUCTIONS_COMMANDS:
            if command_argument:
                console.print("[yellow]用法：/reload-instructions[/yellow]")
            else:
                reloaded = _reload_instruction_manager(
                    console, agent, instruction_manager
                )
                if reloaded is not None:
                    project_instructions = reloaded
            continue
        if command in INIT_COMMANDS:
            if command_argument:
                console.print("[yellow]用法：/init[/yellow]")
            else:
                initialized = _initialize_instructions(
                    console,
                    agent,
                    filesystem_tools.root,
                    instruction_target,
                    project_instructions,
                )
                if initialized is not None:
                    project_instructions = initialized
                    instruction_manager.current = initialized
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
        if command in SESSIONS_COMMANDS:
            _show_sessions(
                console,
                session_store,
                current_session.session_id,
                command_argument.strip(),
            )
            continue
        if command in EXPORT_SESSION_COMMANDS:
            _export_session(
                console,
                session_store,
                command_argument.strip() or current_session.session_id,
            )
            continue
        if command in IMPORT_SESSION_COMMANDS:
            _import_session(console, session_store, command_argument.strip())
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
        if command in RENAME_SESSION_COMMANDS:
            renamed = _rename_session(
                console,
                session_store,
                current_session,
                command_argument,
            )
            if renamed is not None:
                current_session = renamed
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
                snapshot = session_store.save(
                    current_session,
                    agent.messages,
                    task_tracker.steps,
                    task_tracker.latest_quality_check,
                )
                current_session = session_store.handle_for(snapshot)
            except SessionError as error:
                console.print(f"[yellow]本地会话未保存：[/yellow]{error}")


def _show_welcome(
    console: Console,
    model: str,
    thinking_enabled: bool,
    workspace_root: str,
    tool_count: int,
    approval_tool_count: int,
    session_id: str,
    instructions: ProjectInstructions,
) -> None:
    """Render one cohesive startup dashboard before the first prompt."""

    console.print(
        _build_welcome_panel(
            model=model,
            thinking_enabled=thinking_enabled,
            workspace_root=workspace_root,
            tool_count=tool_count,
            approval_tool_count=approval_tool_count,
            session_id=session_id,
            instructions=instructions,
        )
    )


def _build_welcome_panel(
    *,
    model: str,
    thinking_enabled: bool,
    workspace_root: str,
    tool_count: int,
    approval_tool_count: int,
    session_id: str,
    instructions: ProjectInstructions,
) -> Panel:
    """Build a responsive Rich panel without interpolating runtime markup."""

    brand = Text()
    brand.append("◆  NEIL AGENT", style="bold bright_green")
    brand.append("\n   本地 Coding Agent 已准备就绪", style="dim")

    details = Table.grid(expand=True, padding=(0, 2))
    details.add_column(width=10, no_wrap=True, style="dim")
    details.add_column(ratio=1, overflow="fold")
    details.add_row("模型", Text(model, style="cyan"))
    details.add_row("思考模式", "开启" if thinking_enabled else "关闭")
    details.add_row("工作区", Text(workspace_root, overflow="fold"))
    details.add_row("会话", Text(session_id, overflow="fold"))
    details.add_row(
        "工具",
        f"{tool_count} 个可用 · {approval_tool_count} 个操作需要批准",
    )
    details.add_row("项目指令", _welcome_instruction_status(instructions))

    shortcuts = Text("开始使用  ", style="dim")
    for index, command in enumerate(("/help", "/doctor", "/instructions")):
        if index:
            shortcuts.append("  ·  ", style="dim")
        shortcuts.append(command, style="bold cyan")
    shortcuts.append("\nCtrl+C 可取消当前回答，/exit 退出。", style="dim")

    return Panel(
        Group(brand, Text(), details, Text(), shortcuts),
        border_style="bright_green",
        padding=(1, 2),
        subtitle=Text(" local workspace · approval aware ", style="dim"),
    )


def _welcome_instruction_status(instructions: ProjectInstructions) -> Text:
    if instructions.active:
        return Text(
            f"已加载 {len(instructions.active_sources)} 个来源 · "
            f"{instructions.size_bytes} 字节",
            style="green",
        )
    if instructions.status == "invalid":
        return Text(f"未加载 · {instructions.reason}", style="yellow")
    if instructions.status == "empty":
        return Text("AGENTS.md 为空 · 修改后使用 /reload-instructions", style="yellow")
    return Text("未配置 · 使用 /init 创建", style="dim")


def _show_help(console: Console) -> None:
    console.print("[bold]可用命令[/bold]")
    console.print("  /clear  清空对话历史")
    console.print("  /exit   退出程序")
    console.print("  /help   显示帮助")
    console.print("  /context 显示上下文预算和当前占用")
    console.print("  /doctor 检查本地配置和运行环境")
    console.print("  /instructions 显示项目指令加载状态")
    console.print("  /reload-instructions 重新加载项目指令")
    console.print("  /init   预览并创建根 AGENTS.md（仅限不存在时）")
    console.print("  /compact 压缩较早的完整对话轮次")
    console.print("  /sessions [选项] [关键词] 分页、排序或筛选本地会话")
    console.print("  /resume <id> 恢复指定会话")
    console.print("  /export [id] 预览并导出会话（默认当前会话）")
    console.print("  /import <文件名> 预览并导入 exports 目录中的会话")
    console.print("  /rename-session <标题> 重命名当前会话")
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
        f"当前会话\n  {current_session.title or '新会话'}\n"
        f"  {current_session.session_id}\n\n"
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
    arguments: str = "",
) -> None:
    """List selected local sessions without calling the model."""

    try:
        options = _parse_session_list_options(arguments)
        index = session_store.list_sessions(
            options.query,
            page=options.page,
            page_size=options.page_size,
            sort_by=options.sort_by,
            order=options.order,
            state=options.state,
        )
    except SessionError as error:
        console.print(f"[bold red]无法读取本地会话：[/bold red]{error}")
        return

    heading = "本地会话" if not options.query else f"本地会话搜索：{options.query}"
    console.print(heading, style="bold", markup=False, highlight=False)
    console.print(
        f"  存储占用：{_format_bytes(index.total_size_bytes)}；"
        f"有效会话：{index.valid_count} 个；匹配：{index.matched_count} 个",
        markup=False,
        highlight=False,
    )
    console.print(
        f"  第 {index.page} 页 · 每页 {index.page_size} 个 · "
        f"排序 {options.sort_by}/{options.order} · 筛选 {options.state}",
        style="dim",
        markup=False,
        highlight=False,
    )
    if not index.sessions:
        empty_text = "（没有匹配会话）" if options.query else "（尚无已保存会话）"
        console.print(f"  {empty_text}")
    for summary in index.sessions:
        marker = "*" if summary.session_id == current_session_id else " "
        updated_at = summary.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        console.print(
            f"{marker} {summary.title}  {updated_at}  "
            f"{summary.round_count} 轮  {_format_bytes(summary.size_bytes)}",
            markup=False,
            highlight=False,
        )
        console.print(
            f"    {summary.session_id} · {summary.preview}",
            style="dim",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        states = []
        if summary.has_plan:
            states.append("有计划")
        if summary.failed_check:
            states.append("检查失败")
        if summary.has_compaction:
            states.append("已压缩")
        if states:
            console.print(f"    状态：{'、'.join(states)}", style="dim")
    if index.invalid_count:
        console.print(
            f"[yellow]已跳过 {index.invalid_count} 个损坏或不兼容的会话文件。[/yellow]"
        )


def _parse_session_list_options(arguments: str) -> SessionListOptions:
    """Parse a small, deterministic command grammar without model involvement."""

    try:
        tokens = shlex.split(arguments)
    except ValueError as error:
        raise SessionError(f"会话列表参数无效：{error}") from error
    values: dict[str, str] = {}
    query_parts: list[str] = []
    option_names = {
        "--page": "page",
        "--page-size": "page_size",
        "--sort": "sort_by",
        "--order": "order",
        "--state": "state",
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            key = option_names.get(token)
            if key is None or index + 1 >= len(tokens):
                raise SessionError(f"无效或缺少值的会话列表选项：{token}")
            if key in values:
                raise SessionError(f"会话列表选项不能重复：{token}")
            values[key] = tokens[index + 1]
            index += 2
            continue
        query_parts.append(token)
        index += 1
    try:
        page = int(values.get("page", "1"))
        page_size = int(values.get("page_size", "10"))
    except ValueError as error:
        raise SessionError("--page 和 --page-size 必须是整数。") from error
    sort_by = values.get("sort_by", "updated")
    order = values.get("order", "desc")
    state = values.get("state", "all")
    if sort_by not in {"updated", "title"}:
        raise SessionError("--sort 只支持 updated 或 title。")
    if order not in {"asc", "desc"}:
        raise SessionError("--order 只支持 asc 或 desc。")
    if state not in {"all", "planned", "failed", "compacted"}:
        raise SessionError("--state 只支持 all、planned、failed 或 compacted。")
    return SessionListOptions(
        query=" ".join(query_parts),
        page=page,
        page_size=page_size,
        sort_by=cast(SessionSort, sort_by),
        order=cast(SessionOrder, order),
        state=cast(SessionStateFilter, state),
    )


def _export_session(
    console: Console,
    session_store: SessionStore,
    session_id: str,
) -> None:
    """Preview and explicitly approve one local session export."""

    if not session_id:
        console.print("[yellow]用法：/export [session-id][/yellow]")
        return
    try:
        prepared = session_store.prepare_export(session_id)
    except SessionError as error:
        console.print(f"[bold red]无法准备会话导出：[/bold red]{error}")
        return
    console.print("[bold yellow]导出会话预览[/bold yellow]")
    console.print(
        f"  ID：{prepared.summary.session_id}\n"
        f"  标题：{prepared.summary.title}\n"
        f"  轮次：{prepared.summary.round_count}\n"
        f"  大小：{_format_bytes(prepared.size_bytes)}\n"
        f"  目标：{prepared.target}\n"
        "  内容：消息、计划和最近检查；不含 API Key、环境配置或项目指令",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    if not _confirm_local_action(console, "创建该导出文件？[y/N] "):
        console.print("[yellow]已取消导出。[/yellow]")
        return
    try:
        target = session_store.apply_export(prepared)
    except SessionError as error:
        console.print(f"[bold red]会话导出失败：[/bold red]{error}")
        return
    console.print(f"[green]会话已导出：{target}[/green]", markup=False)


def _import_session(
    console: Console,
    session_store: SessionStore,
    filename: str,
) -> None:
    """Preview and explicitly approve one strict local session import."""

    if not filename:
        console.print("[yellow]用法：/import <exports 目录中的文件名>[/yellow]")
        return
    try:
        prepared = session_store.prepare_import(filename)
    except SessionError as error:
        console.print(f"[bold red]无法准备会话导入：[/bold red]{error}")
        return
    console.print("[bold yellow]导入会话预览[/bold yellow]")
    console.print(
        f"  来源：{prepared.source}\n"
        f"  ID：{prepared.summary.session_id}\n"
        f"  标题：{prepared.summary.title}\n"
        f"  轮次：{prepared.summary.round_count}\n"
        f"  大小：{_format_bytes(prepared.size_bytes)}",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    if not _confirm_local_action(console, "导入该会话？[y/N] "):
        console.print("[yellow]已取消导入。[/yellow]")
        return
    try:
        snapshot = session_store.apply_import(prepared)
    except SessionError as error:
        console.print(f"[bold red]会话导入失败：[/bold red]{error}")
        return
    console.print(
        f"[green]会话已导入：{snapshot.session_id}；使用 /resume 恢复。[/green]"
    )


def _confirm_local_action(console: Console, prompt: str) -> bool:
    try:
        answer = console.input(f"[bold yellow]{prompt}[/bold yellow]")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


def _show_context(console: Console, agent: Agent) -> None:
    """Display approximate request usage without calling the model."""

    stats = agent.context_stats()
    selected_total = stats.fixed_chars + stats.selected_message_chars
    selected_tokens = stats.fixed_tokens + stats.selected_message_tokens
    token_budget = (
        f"{stats.budget_tokens:,} token（本地估算）"
        if stats.budget_tokens is not None
        else "未配置（仅使用字符软预算）"
    )
    console.print("[bold]上下文状态[/bold]")
    console.print(
        f"  预算：{stats.budget_chars:,} 字符\n"
        f"  Token 预算：{token_budget}\n"
        f"  固定开销：{stats.fixed_chars:,} 字符（系统提示词和工具定义）\n"
        f"  已保存历史：{stats.stored_rounds} 轮 / {stats.stored_messages} 条消息 / "
        f"约 {stats.stored_message_chars:,} 字符\n"
        f"  下次请求可带历史：{stats.selected_rounds} 轮 / "
        f"{stats.selected_messages} 条消息 / 约 {stats.selected_message_chars:,} 字符\n"
        f"  下次请求基础占用：约 {selected_total:,} 字符 / "
        f"{selected_tokens:,} token",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    if stats.omitted_rounds:
        console.print(
            f"[yellow]  已从下次请求省略 {stats.omitted_rounds} 轮旧历史。[/yellow]"
        )
    console.print(
        "[dim]字符数按 API JSON 计算；token 使用模型无关的保守估算，不等同计费。"
        "下一条用户输入也会占用预算。"
        "当前请求及其工具链不会被截断。[/dim]"
    )


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
        f"  目标：{instructions.target}\n"
        f"  合计：{instructions.size_bytes} 字节 / {instructions.char_count} 字符",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    for index, source in enumerate(instructions.active_sources, start=1):
        console.print(
            f"  {index}. {source.source}（{source.size_bytes} 字节，"
            f"作用域：{source.scope}）",
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


def _reload_instructions(
    console: Console,
    agent: Agent,
    workspace_root: Path,
    instruction_target: Path,
    current: ProjectInstructions,
) -> ProjectInstructions | None:
    """Reload a complete instruction chain, retaining the old snapshot on failure."""

    try:
        candidate = load_project_instructions(workspace_root, instruction_target)
    except ValueError as error:
        console.print(f"[bold red]项目指令重新加载失败：[/bold red]{error}")
        console.print("[yellow]继续使用旧的有效指令快照。[/yellow]")
        return None
    if candidate.status == "invalid":
        console.print(f"[bold red]项目指令重新加载失败：[/bold red]{candidate.reason}")
        if current.active:
            console.print("[yellow]继续使用旧的有效指令快照。[/yellow]")
        else:
            console.print("[yellow]模型上下文仍不包含项目指令。[/yellow]")
        return None

    agent.set_project_instructions(candidate.prompt_section())
    if candidate.active:
        console.print(
            f"[green]已重新加载 {len(candidate.active_sources)} 个项目指令文件。[/green]"
        )
    else:
        console.print("[green]重新加载完成；当前没有生效的项目指令。[/green]")
    return candidate


def _reload_instruction_manager(
    console: Console,
    agent: Agent,
    manager: ProjectInstructionManager,
) -> ProjectInstructions | None:
    """Reload the manager's active scope while retaining its old snapshot."""

    current = manager.current
    try:
        candidate = manager.reload()
    except NeilAgentError as error:
        console.print(f"[bold red]项目指令重新加载失败：[/bold red]{error}")
        if current.active:
            console.print("[yellow]继续使用旧的有效指令快照。[/yellow]")
        else:
            console.print("[yellow]模型上下文仍不包含项目指令。[/yellow]")
        return None
    agent.set_project_instructions(candidate.prompt_section())
    if candidate.active:
        console.print(
            f"[green]已重新加载 {len(candidate.active_sources)} 个项目指令文件。[/green]"
        )
    else:
        console.print("[green]重新加载完成；当前没有生效的项目指令。[/green]")
    return candidate


def _initialize_instructions(
    console: Console,
    agent: Agent,
    workspace_root: Path,
    instruction_target: Path,
    current: ProjectInstructions,
) -> ProjectInstructions | None:
    """Preview and explicitly approve a non-overwriting root instruction draft."""

    try:
        candidate = prepare_project_instructions_init(workspace_root)
    except NeilAgentError as error:
        console.print(f"[bold red]无法初始化项目指令：[/bold red]{error}")
        return None

    console.print("\n[bold yellow]创建根目录 AGENTS.md[/bold yellow]")
    console.print(
        f"目标：{candidate.source}\n大小：{candidate.size_bytes} 字节",
        markup=False,
        highlight=False,
    )
    console.print(candidate.preview, markup=False, highlight=False, soft_wrap=True)
    try:
        answer = console.input("[bold yellow]创建该文件？[y/N][/bold yellow] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() not in {"y", "yes"}:
        console.print("[yellow]已取消创建。[/yellow]")
        return None

    try:
        apply_project_instructions_init(candidate)
    except NeilAgentError as error:
        console.print(f"[bold red]创建项目指令失败：[/bold red]{error}")
        return None
    console.print(
        f"已创建：{candidate.source}",
        style="green",
        markup=False,
        highlight=False,
    )
    reloaded = _reload_instructions(
        console,
        agent,
        workspace_root,
        instruction_target,
        current,
    )
    if reloaded is not None:
        console.print("[green]AGENTS.md 已创建并加入当前模型上下文。[/green]")
    else:
        console.print(
            "[yellow]根 AGENTS.md 已创建，但完整指令链未能加载；"
            "请修复提示的问题后运行 /reload-instructions。[/yellow]"
        )
    return reloaded


def _instruction_target(workspace_root: Path) -> Path:
    """Use the launch directory when it is safely inside the configured workspace."""

    try:
        current = Path.cwd().resolve(strict=True)
        current.relative_to(workspace_root)
    except (OSError, ValueError):
        return workspace_root
    return current if current.is_dir() else workspace_root


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


def _rename_session(
    console: Console,
    session_store: SessionStore,
    current_session: SessionHandle,
    title: str,
) -> SessionHandle | None:
    """Rename the current session locally without calling the model."""

    try:
        normalized = normalize_session_title(title)
    except SessionError as error:
        console.print(
            f"[yellow]用法：/rename-session <标题>（最多 80 个字符）[/yellow]\n{error}"
        )
        return None

    if not session_store.has_saved(current_session.session_id):
        renamed = replace(current_session, title=normalized)
        console.print(
            f"当前会话已命名为：{normalized}（首次回答后保存）",
            style="green",
            markup=False,
            highlight=False,
        )
        return renamed
    try:
        snapshot = session_store.rename(current_session.session_id, normalized)
    except SessionError as error:
        console.print(f"[bold red]重命名会话失败：[/bold red]{error}")
        return None
    console.print(
        f"当前会话已重命名为：{snapshot.title}",
        style="green",
        markup=False,
        highlight=False,
    )
    return session_store.handle_for(snapshot)


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
        f"已恢复本地会话：{snapshot.title} · {snapshot.session_id}"
        f"（{len(snapshot.messages)} 条消息）",
        style="green",
        markup=False,
        highlight=False,
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
