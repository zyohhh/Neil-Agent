"""Read-only Rich cockpit for current Neil Agent runtime state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unicodedata import category

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .context import ContextStats
from .schemas import TokenUsage
from .task import QualityCheckRecord, TaskStep

COCKPIT_METER_WIDTH = 24
MAX_COCKPIT_VALUE_CHARS = 200


@dataclass(frozen=True, slots=True)
class CockpitSnapshot:
    """Bounded runtime metadata rendered without reading hidden content."""

    model: str
    thinking_enabled: bool
    workspace: Path
    session_id: str
    session_title: str
    context: ContextStats
    last_usage: TokenUsage | None
    plan: tuple[TaskStep, ...]
    latest_quality_check: QualityCheckRecord | None
    tool_count: int
    approval_tool_count: int
    instruction_status: str
    instruction_sources: int
    instruction_bytes: int
    checkpoint_count: int
    audit_enabled: bool
    git_branch: str
    git_changes: int
    git_available: bool = True


def build_cockpit_panel(snapshot: CockpitSnapshot) -> Panel:
    """Build a responsive mission-control snapshot for an interactive CLI."""

    header = Text()
    header.append("◆  NEIL // MISSION CONTROL", style="bold bright_cyan")
    header.append("\n   READ-ONLY RUNTIME COCKPIT", style="dim")

    identity = Table.grid(expand=True, padding=(0, 2))
    identity.add_column(width=10, no_wrap=True, style="dim")
    identity.add_column(ratio=1, overflow="fold")
    identity.add_row("模型", _value(snapshot.model, style="cyan"))
    identity.add_row(
        "思考模式",
        Text("ONLINE" if snapshot.thinking_enabled else "OFFLINE", style="green"),
    )
    identity.add_row("会话", _value(snapshot.session_title or "新会话", style="white"))
    identity.add_row("会话 ID", _value(snapshot.session_id, style="dim"))
    identity.add_row("工作区", _value(str(snapshot.workspace)))

    sections = Table.grid(expand=True, padding=(0, 1))
    sections.add_column(ratio=1, overflow="fold")
    sections.add_row(_section("TASK MATRIX", _task_matrix(snapshot)))
    sections.add_row(_section("CONTEXT TOMOGRAPHY · BASIC", _context_view(snapshot)))
    sections.add_row(_section("SECURITY SHIELD · BASIC", _security_view(snapshot)))
    sections.add_row(_section("WORKSPACE SIGNAL", _workspace_view(snapshot)))

    footer = Text()
    footer.append("/context", style="bold cyan")
    footer.append(" 详细预算  ·  ", style="dim")
    footer.append("/permissions", style="bold cyan")
    footer.append(" 完整边界  ·  ", style="dim")
    footer.append("/status", style="bold cyan")
    footer.append(" 任务与 Git", style="dim")

    return Panel(
        Group(header, Text(), identity, Text(), sections, Text(), footer),
        border_style="bright_cyan",
        padding=(1, 2),
        title=Text(" live system snapshot ", style="bold bright_cyan"),
        subtitle=Text(" local · metadata only · no model call ", style="dim"),
    )


def _section(title: str, body: RenderableType) -> Panel:
    return Panel(
        body,
        title=Text(f" {title} ", style="bold bright_magenta"),
        border_style="dim cyan",
        padding=(0, 1),
    )


def _task_matrix(snapshot: CockpitSnapshot) -> RenderableType:
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(width=3, no_wrap=True)
    table.add_column(ratio=1, overflow="fold")
    table.add_column(width=12, no_wrap=True, justify="right")
    if not snapshot.plan:
        table.add_row("○", Text("尚未创建任务计划", style="dim"), "IDLE")
    else:
        markers = {
            "pending": ("○", "dim", "PENDING"),
            "in_progress": ("●", "bold bright_cyan", "RUNNING"),
            "completed": ("✓", "green", "COMPLETE"),
        }
        for step in snapshot.plan:
            marker, style, label = markers[step.status]
            table.add_row(
                Text(marker, style=style),
                _value(step.title, style=style),
                Text(label, style=style),
            )
    quality = snapshot.latest_quality_check
    if quality is not None:
        quality_style = {
            "passed": "green",
            "failed": "bold red",
            "not_run": "yellow",
        }[quality.status]
        table.add_row(
            Text("◆", style=quality_style),
            Text(f"最近检查：{_safe_value(quality.check)}", style=quality_style),
            Text(quality.status.upper(), style=quality_style),
        )
    return table


def _context_view(snapshot: CockpitSnapshot) -> RenderableType:
    stats = snapshot.context
    chars_used = stats.fixed_chars + stats.selected_message_chars
    tokens_used = stats.fixed_tokens + stats.selected_message_tokens
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(width=12, no_wrap=True, style="dim")
    table.add_column(ratio=1, overflow="fold")
    table.add_row(
        "字符软预算",
        _meter(chars_used, stats.budget_chars, style="bright_cyan"),
    )
    if stats.budget_tokens is None:
        table.add_row(
            "Token 估算",
            Text(f"{tokens_used:,} · 未配置 token 软上限", style="cyan"),
        )
    else:
        table.add_row(
            "Token 软预算",
            _meter(tokens_used, stats.budget_tokens, style="bright_magenta"),
        )
    table.add_row(
        "上下文构成",
        Text(
            f"固定 {stats.fixed_chars:,} 字符 · "
            f"历史 {stats.selected_message_chars:,} 字符 · "
            f"保留 {stats.selected_rounds}/{stats.stored_rounds} 轮"
        ),
    )
    if stats.omitted_rounds:
        table.add_row(
            "裁剪预警",
            Text(f"{stats.omitted_rounds} 轮旧历史不会进入下次请求", style="yellow"),
        )
    usage = snapshot.last_usage
    if usage is None:
        table.add_row("服务端实测", Text("暂无", style="dim"))
    else:
        table.add_row(
            "服务端实测",
            Text(
                f"IN {usage.input_tokens:,} · OUT {usage.output_tokens:,} · "
                f"TOTAL {usage.total_tokens:,}",
                style="green",
            ),
        )
    return table


def _security_view(snapshot: CockpitSnapshot) -> RenderableType:
    direct_count = max(snapshot.tool_count - snapshot.approval_tool_count, 0)
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(width=12, no_wrap=True, style="dim")
    table.add_column(ratio=1, overflow="fold")
    table.add_row(
        "工具权限",
        Text(
            f"DIRECT {direct_count} · APPROVAL {snapshot.approval_tool_count}",
            style="green" if snapshot.approval_tool_count == 0 else "yellow",
        ),
    )
    table.add_row(
        "文件边界",
        Text("WORKSPACE LOCKED · SENSITIVE PATHS BLOCKED", style="green"),
    )
    table.add_row(
        "本地审计",
        Text(
            "RECORDING METADATA" if snapshot.audit_enabled else "DISABLED",
            style="green" if snapshot.audit_enabled else "dim",
        ),
    )
    table.add_row(
        "OS 沙箱",
        Text("NOT ACTIVE · COMMAND ALLOWLIST ONLY", style="yellow"),
    )
    return table


def _workspace_view(snapshot: CockpitSnapshot) -> RenderableType:
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(width=12, no_wrap=True, style="dim")
    table.add_column(ratio=1, overflow="fold")
    instruction_style = "green" if snapshot.instruction_status == "active" else "yellow"
    table.add_row(
        "项目指令",
        Text(
            f"{snapshot.instruction_status.upper()} · "
            f"{snapshot.instruction_sources} SOURCES · "
            f"{snapshot.instruction_bytes:,} BYTES",
            style=instruction_style,
        ),
    )
    table.add_row(
        "文件检查点",
        Text(f"{snapshot.checkpoint_count} IN-MEMORY", style="cyan"),
    )
    if snapshot.git_available:
        table.add_row(
            "Git 信号",
            Text(
                f"{_safe_value(snapshot.git_branch)} · {snapshot.git_changes} CHANGES",
                style="yellow" if snapshot.git_changes else "green",
            ),
        )
    else:
        table.add_row("Git 信号", Text("UNAVAILABLE", style="yellow"))
    return table


def _meter(value: int, total: int, *, style: str) -> Text:
    bounded_total = max(total, 1)
    ratio = min(max(value / bounded_total, 0.0), 1.0)
    filled = round(ratio * COCKPIT_METER_WIDTH)
    meter = Text()
    meter.append("━" * filled, style=style)
    meter.append("─" * (COCKPIT_METER_WIDTH - filled), style="dim")
    meter.append(f" {value:,}/{total:,} · {ratio * 100:4.1f}%", style="dim")
    return meter


def _value(value: object, *, style: str | None = None) -> Text:
    safe_value = _safe_value(value)
    if style is None:
        return Text(safe_value, overflow="fold")
    return Text(safe_value, style=style, overflow="fold")


def _safe_value(value: object) -> str:
    normalized = " ".join(str(value).split())[:MAX_COCKPIT_VALUE_CHARS]
    return (
        "".join(
            character
            for character in normalized
            if not category(character).startswith("C")
        )
        or "N/A"
    )
