"""Full-screen Textual cockpit driven by runtime-event projections."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event, Lock
from typing import Any, Literal, Protocol
from unicodedata import category

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import Button, ContentSwitcher, Footer, Input, Log, Static, Tree
from textual.widgets.tree import TreeNode

from .context import (
    MAX_CONTEXT_WHAT_IF_CHARS,
    ContextBudgetPressure,
    ContextLayerKind,
    ContextTomography,
    ContextWhatIf,
    context_budget_pressure,
)
from .checkpoint import PreparedFileRestore
from .errors import ToolError
from .events import EventBus, EventSubscription, RuntimeEvent
from .projections import (
    ExecutionGraph,
    ExecutionGraphProjector,
    ExecutionNode,
    MetricsProjector,
    RuntimeMetrics,
)
from .schemas import TokenUsage, ToolCall
from .security import (
    MAX_SECURITY_BOUNDARY_ALERTS,
    ApprovalFlow,
    ApprovalFlowProjector,
    ApprovalTrace,
    CapabilityState,
    SecurityBoundaryAlert,
    SecurityBoundarySignal,
    SecurityBoundaryWatch,
    SecurityShield,
    project_security_boundary_watch,
    project_security_shield,
)
from .time_machine import (
    TimeMachineHistory,
    TimeMachineHistoryProjection,
    TimeMachineProjector,
    TimeMachineSelection,
    TimeMachineSnapshot,
    render_time_machine_snapshot,
)
from .time_machine_restore import can_offer_checkpoint_restore, checkpoint_restore_hint
from .tools.filesystem import FileSystemTools, MAX_TASK_RESTORE_PREVIEW_CHARS

MAX_LIVE_EVENTS = 10_000
MAX_BRIDGE_EVENTS = 1_024
MAX_LIVE_OUTPUT_LINES = 500
MAX_LIVE_ERROR_CHARS = 500
MAX_APPROVAL_PREVIEW_CHARS = 20_000
MAX_SECURITY_OBSERVATIONS = 64
NARROW_TERMINAL_WIDTH = 88
SHORT_TERMINAL_HEIGHT = 36

NodeFilter = Literal["all", "active", "failed", "tools"]
PrimaryMonitorView = Literal["execution", "context"]
MonitorView = Literal["execution", "context", "security", "time-machine"]

_FILTER_LABELS: dict[NodeFilter, str] = {
    "all": "ALL",
    "active": "ACTIVE",
    "failed": "FAILED",
    "tools": "TOOLS",
}
_STATUS_STYLE = {
    "started": "bold bright_cyan",
    "waiting": "bold yellow",
    "succeeded": "green",
    "skipped": "dim yellow",
    "failed": "bold red",
}
_STATUS_MARKER = {
    "started": "◆",
    "waiting": "◇",
    "succeeded": "●",
    "skipped": "○",
    "failed": "▲",
}
_CONTEXT_LAYER_LABELS: dict[ContextLayerKind, str] = {
    "system": "SYSTEM FIXED",
    "tool_schemas": "TOOL SCHEMAS",
    "project_instructions": "PROJECT RULES",
    "selected_history": "HISTORY KEPT",
    "current_chain": "CURRENT CHAIN",
}
_CONTEXT_LAYER_STYLES: dict[ContextLayerKind, str] = {
    "system": "#91f5e9",
    "tool_schemas": "#d7b7ff",
    "project_instructions": "#ffb454",
    "selected_history": "#9ee37d",
    "current_chain": "#68b5ff",
}
_CONTEXT_LAYER_UNITS: dict[ContextLayerKind, str] = {
    "system": "BLOCK",
    "tool_schemas": "DEFS",
    "project_instructions": "BLOCK",
    "selected_history": "MSGS",
    "current_chain": "MSGS",
}
_CONTEXT_PRESSURE_LABELS = {
    "safe": "SAFE",
    "warning": "WATCH",
    "critical": "HIGH",
    "exceeded": "OVER",
}
_CONTEXT_PRESSURE_STYLES = {
    "safe": "bold #9ee37d",
    "warning": "bold yellow",
    "critical": "bold #ffb454",
    "exceeded": "bold red",
}
_CONTEXT_PRESSURE_BORDER_COLORS = {
    "safe": "#277c6f",
    "warning": "#c49a24",
    "critical": "#ffb454",
    "exceeded": "#d72d5b",
}
_SECURITY_STATE_LABELS: dict[CapabilityState, str] = {
    "direct": "DIRECT",
    "approval": "APPROVAL",
    "forbidden": "FORBIDDEN",
    "unavailable": "UNAVAILABLE",
}
_SECURITY_STATE_STYLES: dict[CapabilityState, str] = {
    "direct": "bold #9ee37d",
    "approval": "bold #ffb454",
    "forbidden": "bold #ff4f7d",
    "unavailable": "dim",
}
_SECURITY_STATE_MARKERS: dict[CapabilityState, str] = {
    "direct": "●",
    "approval": "◆",
    "forbidden": "■",
    "unavailable": "○",
}
_APPROVAL_DECISION_LABELS = {
    "pending": "PENDING",
    "approved": "APPROVED",
    "rejected": "REJECTED",
    "unavailable": "UNAVAILABLE",
    "error": "ERROR",
}
_PREVIEW_BINDING_LABELS = {
    "pending": "BINDING PENDING",
    "valid": "BINDING VALID",
    "changed": "BINDING CHANGED",
    "unavailable": "BINDING UNAVAILABLE",
    "not_checked": "NOT CHECKED",
}
_SECURITY_BOUNDARY_LABELS = {
    "path": "PATH",
    "network": "NETWORK",
    "command": "COMMAND",
    "audit": "AUDIT",
}


class LiveAgent(Protocol):
    """Agent surface needed by the live cockpit."""

    def stream_chat(self, user_input: str) -> Iterator[str]: ...

    def context_tomography(self, current_input: str = "") -> ContextTomography: ...

    def context_what_if(self, additional_chars: int) -> ContextWhatIf: ...


class RuntimeEventsReady(Message):
    """A coalesced batch is ready in the thread-safe bridge."""


class AssistantChunk(Message):
    """One plain assistant response fragment from the Agent worker."""

    def __init__(self, chunk: str) -> None:
        super().__init__()
        self.chunk = chunk


class TurnFinished(Message):
    """The background Agent turn reached a stable terminal state."""

    def __init__(self, *, succeeded: bool, cancelled: bool = False) -> None:
        super().__init__()
        self.succeeded = succeeded
        self.cancelled = cancelled


@dataclass(slots=True)
class _ApprovalDecision:
    ready: Event = field(default_factory=Event)
    approved: bool = False


class ApprovalRequested(Message):
    """Request a decision on the Textual message-pump thread."""

    def __init__(
        self,
        *,
        tool_name: str,
        preview: str,
        decision: _ApprovalDecision,
    ) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.preview = preview
        self.decision = decision


class RuntimeEventBridge:
    """Coalesce observer-thread events into bounded UI-thread batches."""

    def __init__(
        self,
        notify: Callable[[], object],
        *,
        max_events: int = MAX_BRIDGE_EVENTS,
    ) -> None:
        if max_events < 1:
            raise ValueError("live event bridge capacity must be at least 1")
        self._notify = notify
        self._events: deque[RuntimeEvent] = deque(maxlen=max_events)
        self._lock = Lock()
        self._notification_pending = False
        self._dropped_events = 0
        self._closed = False

    @property
    def dropped_events(self) -> int:
        with self._lock:
            return self._dropped_events

    def observe(self, event: RuntimeEvent) -> None:
        should_notify = False
        with self._lock:
            if self._closed:
                return
            if len(self._events) == self._events.maxlen:
                self._dropped_events += 1
            self._events.append(event)
            if not self._notification_pending:
                self._notification_pending = True
                should_notify = True
        if should_notify:
            self._notify()

    def drain(self) -> tuple[RuntimeEvent, ...]:
        with self._lock:
            events = tuple(self._events)
            self._events.clear()
            self._notification_pending = False
            return events

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._events.clear()
            self._notification_pending = False


class ToolApprovalScreen(ModalScreen[bool]):
    """Explicit yes/no approval without leaving the full-screen cockpit."""

    BINDINGS = [
        Binding("y", "approve", "批准", priority=True),
        Binding("n,escape", "reject", "拒绝", priority=True),
    ]

    DEFAULT_CSS = """
    ToolApprovalScreen {
        align: center middle;
        background: rgba(3, 8, 14, 0.82);
    }

    #approval-dialog {
        width: 82%;
        max-width: 110;
        height: 78%;
        padding: 1 2;
        background: #101923;
        border: heavy #ffb454;
    }

    #approval-kicker {
        height: 1;
        color: #ffb454;
        text-style: bold;
    }

    #approval-title {
        height: 2;
        margin-bottom: 1;
        color: #f4f7fb;
        text-style: bold;
    }

    #approval-preview {
        height: 1fr;
        padding: 1;
        background: #080d14;
        border: round #34495e;
        color: #d9e2ec;
    }

    #approval-actions {
        height: 3;
        margin-top: 1;
        align-horizontal: right;
    }

    #approve {
        margin-right: 1;
        background: #127d68;
    }
    """

    def __init__(self, tool_name: str, preview: str) -> None:
        super().__init__()
        self._tool_name = _safe_inline(tool_name)
        self._preview = _safe_multiline(preview, MAX_APPROVAL_PREVIEW_CHARS)

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog"):
            yield Static("HIGH-RISK TOOL REQUEST", id="approval-kicker")
            yield Static(
                Text(f"{self._tool_name} 需要你的明确批准", style="bold"),
                id="approval-title",
            )
            with VerticalScroll(id="approval-preview"):
                yield Static(Text(self._preview))
            with Horizontal(id="approval-actions"):
                yield Button("批准  Y", id="approve", variant="success")
                yield Button("拒绝  N", id="reject", variant="error")

    @on(Button.Pressed, "#approve")
    def approve_button(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#reject")
    def reject_button(self) -> None:
        self.dismiss(False)

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)


class CheckpointRestoreScreen(ModalScreen[bool]):
    """Explicit yes/no approval for the latest task checkpoint restore."""

    BINDINGS = [
        Binding("y", "approve", "恢复", priority=True),
        Binding("n,escape", "reject", "取消", priority=True),
    ]

    DEFAULT_CSS = """
    CheckpointRestoreScreen {
        align: center middle;
        background: rgba(3, 8, 14, 0.82);
    }

    #checkpoint-restore-dialog {
        width: 82%;
        max-width: 110;
        height: 78%;
        padding: 1 2;
        background: #101923;
        border: heavy #ff7f50;
    }

    #checkpoint-restore-kicker {
        height: 1;
        color: #ff7f50;
        text-style: bold;
    }

    #checkpoint-restore-title {
        height: 2;
        margin-bottom: 1;
        color: #f4f7fb;
        text-style: bold;
    }

    #checkpoint-restore-preview {
        height: 1fr;
        padding: 1;
        background: #080d14;
        border: round #34495e;
        color: #d9e2ec;
    }

    #checkpoint-restore-actions {
        height: 3;
        margin-top: 1;
        align-horizontal: right;
    }

    #checkpoint-restore-approve {
        margin-right: 1;
        background: #127d68;
    }
    """

    def __init__(self, checkpoint_id: str, preview: str) -> None:
        super().__init__()
        self._checkpoint_id = _safe_inline(checkpoint_id)
        self._preview = _safe_multiline(preview, MAX_TASK_RESTORE_PREVIEW_CHARS)

    def compose(self) -> ComposeResult:
        with Vertical(id="checkpoint-restore-dialog"):
            yield Static("TASK CHECKPOINT RESTORE", id="checkpoint-restore-kicker")
            yield Static(
                Text(
                    f"恢复检查点 {self._checkpoint_id} 需要你的明确批准",
                    style="bold",
                ),
                id="checkpoint-restore-title",
            )
            with VerticalScroll(id="checkpoint-restore-preview"):
                yield Static(Text(self._preview))
            with Horizontal(id="checkpoint-restore-actions"):
                yield Button("恢复  Y", id="checkpoint-restore-approve", variant="success")
                yield Button("取消  N", id="checkpoint-restore-reject", variant="error")

    @on(Button.Pressed, "#checkpoint-restore-approve")
    def approve_button(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#checkpoint-restore-reject")
    def reject_button(self) -> None:
        self.dismiss(False)

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)


class ContextWhatIfScreen(ModalScreen[int | None]):
    """Collect a bounded character count for a local-only simulation."""

    BINDINGS = [Binding("escape", "cancel", "取消", priority=True)]

    DEFAULT_CSS = """
    ContextWhatIfScreen {
        align: center middle;
        background: rgba(3, 8, 14, 0.82);
    }

    #what-if-dialog {
        width: 88%;
        max-width: 72;
        height: auto;
        max-height: 20;
        padding: 1 2;
        background: #0c1820;
        border: heavy #ffb454;
    }

    #what-if-kicker {
        height: 1;
        color: #ffb454;
        text-style: bold;
    }

    #what-if-title {
        height: 2;
        color: #f4f7fb;
        text-style: bold;
    }

    #what-if-copy,
    #what-if-hint {
        height: auto;
        margin-bottom: 1;
        color: #8fa9bd;
    }

    #what-if-input {
        height: 3;
        margin-bottom: 1;
        border: tall #1e6f78;
        background: #09131a;
    }

    #what-if-input:focus {
        border: tall #00d4c7;
    }

    #what-if-actions {
        height: 3;
        align-horizontal: right;
    }

    #what-if-apply,
    #what-if-clear {
        margin-right: 1;
    }
    """

    def __init__(self, initial_chars: int = 10_000) -> None:
        super().__init__()
        self._initial_chars = min(max(initial_chars, 1), MAX_CONTEXT_WHAT_IF_CHARS)

    def compose(self) -> ComposeResult:
        with Vertical(id="what-if-dialog"):
            yield Static("LOCAL WHAT-IF · NO MODEL CALL", id="what-if-kicker")
            yield Static("如果下一次输入增加 N 个字符", id="what-if-title")
            yield Static(
                "只重算本地预算与完整轮次裁剪；按 ASCII 约 0.3 token/字符估算。",
                id="what-if-copy",
            )
            yield Input(
                str(self._initial_chars),
                placeholder=f"1–{MAX_CONTEXT_WHAT_IF_CHARS:,}",
                type="integer",
                max_length=len(str(MAX_CONTEXT_WHAT_IF_CHARS)),
                id="what-if-input",
            )
            yield Static(
                f"范围 1–{MAX_CONTEXT_WHAT_IF_CHARS:,}；输入 0 可清除当前模拟。",
                id="what-if-hint",
            )
            with Horizontal(id="what-if-actions"):
                yield Button("模拟  Enter", id="what-if-apply", variant="warning")
                yield Button("清除", id="what-if-clear")
                yield Button("取消  Esc", id="what-if-cancel")

    def on_mount(self) -> None:
        self.query_one("#what-if-input", Input).focus()

    @on(Input.Submitted, "#what-if-input")
    def submit_value(self, event: Input.Submitted) -> None:
        self._submit(event.value)

    @on(Button.Pressed, "#what-if-apply")
    def apply_button(self) -> None:
        self._submit(self.query_one("#what-if-input", Input).value)

    @on(Button.Pressed, "#what-if-clear")
    def clear_button(self) -> None:
        self.dismiss(0)

    @on(Button.Pressed, "#what-if-cancel")
    def cancel_button(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self, raw_value: str) -> None:
        value = raw_value.strip()
        if not value.isdecimal():
            self.notify("请输入非负整数", severity="warning")
            return
        additional_chars = int(value)
        if additional_chars == 0:
            self.dismiss(0)
            return
        if additional_chars > MAX_CONTEXT_WHAT_IF_CHARS:
            self.notify(
                f"模拟上限为 {MAX_CONTEXT_WHAT_IF_CHARS:,} 字符",
                severity="warning",
            )
            return
        self.dismiss(additional_chars)


class LiveCockpitApp(App[None]):
    """Interactive Agent shell with a metadata-only live execution tree."""

    TITLE = "Neil // Live Mission Control"
    SUB_TITLE = "metadata-only runtime projection"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("ctrl+q", "request_exit", "退出"),
        Binding("ctrl+x", "cancel_turn", "取消请求"),
        Binding(
            "f2",
            "toggle_output",
            "切换结果",
            priority=True,
            tooltip="展开结果区或返回实时执行树",
        ),
        Binding("ctrl+o", "toggle_output", "", show=False, priority=True),
        Binding(
            "f3",
            "toggle_monitor",
            "DAG/上下文",
            priority=True,
            tooltip="切换实时执行树与上下文断层图",
        ),
        Binding("ctrl+t", "toggle_monitor", "", show=False, priority=True),
        Binding(
            "f4",
            "context_what_if",
            "上下文模拟",
            priority=True,
            tooltip="本地模拟下一次输入增加 N 个字符",
        ),
        Binding(
            "f5",
            "toggle_security",
            "安全盾",
            priority=True,
            tooltip="查看应用权限色带与独立 OS 沙箱边界",
        ),
        Binding(
            "f6",
            "toggle_time_machine",
            "时间机器",
            priority=True,
            tooltip="浏览事件/会话/检查点；最新检查点可按 R 经审批恢复",
        ),
        Binding(
            "r",
            "restore_checkpoint",
            "恢复检查点",
            priority=True,
            tooltip="预览并批准恢复最新任务检查点",
        ),
        Binding("1", "filter_all", "全部"),
        Binding("2", "filter_active", "进行中"),
        Binding("3", "filter_failed", "失败"),
        Binding("4", "filter_tools", "工具"),
    ]

    CSS = """
    Screen {
        background: #070b11;
        color: #d9e2ec;
    }

    #brand {
        height: 4;
        padding: 1 2;
        background: #0d1620;
        border-bottom: heavy #00d4c7;
        color: #f4f7fb;
        text-style: bold;
    }

    #metrics {
        height: 3;
        padding: 1 2;
        background: #0a111a;
        color: #8fa9bd;
    }

    #workspace {
        height: 1fr;
        min-height: 14;
        padding: 0 1;
    }

    .workspace-view {
        width: 1fr;
        height: 1fr;
    }

    #dag-panel {
        width: 2fr;
        min-width: 40;
        margin-right: 1;
        border: round #1e6f78;
        background: #0a1018;
    }

    #detail-panel {
        width: 1fr;
        min-width: 28;
        border: round #573c78;
        background: #0e1019;
    }

    #context-panel {
        width: 2fr;
        min-width: 40;
        margin-right: 1;
        border: round #277c6f;
        background: #09131a;
    }

    #context-detail-panel {
        width: 1fr;
        min-width: 28;
        border: round #315b7a;
        background: #0b111c;
    }

    #security-panel {
        width: 2fr;
        min-width: 40;
        margin-right: 1;
        border: round #7d5b24;
        background: #11130f;
    }

    #security-detail-panel {
        width: 1fr;
        min-width: 28;
        border: round #74405b;
        background: #130f17;
    }

    #time-machine-panel {
        width: 2fr;
        min-width: 40;
        margin-right: 1;
        border: round #356c94;
        background: #09121b;
    }

    #time-machine-detail-panel {
        width: 1fr;
        min-width: 28;
        border: round #5e4f91;
        background: #100f1b;
    }

    .panel-title {
        height: 2;
        padding: 0 1;
        color: #91f5e9;
        background: #101d28;
        text-style: bold;
    }

    #detail-panel .panel-title {
        color: #d7b7ff;
        background: #191426;
    }

    #context-panel .panel-title {
        color: #9eeadf;
        background: #10231f;
    }

    #context-detail-panel .panel-title {
        color: #9bcdf5;
        background: #111d2b;
    }

    #security-panel .panel-title {
        color: #ffd08a;
        background: #211b10;
    }

    #security-detail-panel .panel-title {
        color: #ff9fba;
        background: #241421;
    }

    #time-machine-panel .panel-title {
        color: #9bcdf5;
        background: #102033;
    }

    #time-machine-detail-panel .panel-title {
        color: #c8b8ff;
        background: #1b1730;
    }

    #execution-tree {
        height: 1fr;
        padding: 0 1 1 1;
        scrollbar-color: #1e6f78;
        scrollbar-background: #0a1018;
    }

    #node-detail {
        height: 1fr;
        padding: 1 2;
        color: #c6d4df;
        overflow-y: auto;
    }

    #context-layers {
        height: auto;
        padding: 1 2 0 2;
        color: #c6d4df;
    }

    #context-insights,
    #context-detail {
        height: 1fr;
        padding: 1 2;
        color: #c6d4df;
        overflow-y: auto;
        scrollbar-color: #277c6f;
        scrollbar-background: #09131a;
    }

    #security-capabilities {
        height: auto;
        padding: 1 2 0 2;
        color: #c6d4df;
    }

    #security-watch {
        display: none;
        height: auto;
        padding: 0 2;
        color: #c6d4df;
    }

    #approval-flows,
    #security-detail {
        height: 1fr;
        padding: 1 2;
        color: #c6d4df;
        overflow-y: auto;
        scrollbar-color: #7d5b24;
        scrollbar-background: #11130f;
    }

    #time-machine-tree {
        height: 1fr;
        padding: 0 1 1 1;
        scrollbar-color: #356c94;
        scrollbar-background: #09121b;
    }

    #time-machine-inline-detail {
        display: none;
        height: auto;
        max-height: 9;
        padding: 0 1 1 1;
        color: #c6d4df;
        overflow-y: auto;
    }

    #time-machine-detail {
        height: 1fr;
        padding: 1 2;
        color: #c6d4df;
        overflow-y: auto;
        scrollbar-color: #5e4f91;
        scrollbar-background: #100f1b;
    }

    #conversation {
        height: 1fr;
        min-height: 14;
        margin: 0 1;
        border: round #29485c;
        background: #080d14;
    }

    #stream-title {
        height: 2;
        padding: 0 1;
        color: #8fa9bd;
        background: #0d1620;
        content-align: left middle;
    }

    #transcript {
        height: 1fr;
        padding: 0 1;
        scrollbar-color: #29485c;
        scrollbar-background: #080d14;
    }

    #prompt {
        height: 3;
        border: tall #1e6f78;
        background: #0d1620;
    }

    #prompt:focus {
        border: tall #00d4c7;
    }

    Footer {
        background: #0d1620;
        color: #71879a;
    }

    LiveCockpitApp.narrow #dag-panel,
    LiveCockpitApp.narrow #context-panel,
    LiveCockpitApp.narrow #security-panel,
    LiveCockpitApp.narrow #time-machine-panel {
        width: 1fr;
        min-width: 0;
        margin-right: 0;
    }

    LiveCockpitApp.narrow #detail-panel,
    LiveCockpitApp.narrow #context-detail-panel,
    LiveCockpitApp.narrow #security-detail-panel,
    LiveCockpitApp.narrow #time-machine-detail-panel {
        display: none;
    }

    LiveCockpitApp.narrow #time-machine-inline-detail {
        display: block;
    }

    LiveCockpitApp.narrow #approval-flows {
        height: auto;
        padding: 0 2 1 2;
    }

    LiveCockpitApp.narrow #security-watch {
        display: block;
    }

    LiveCockpitApp.narrow #brand {
        height: 3;
        padding: 0 1;
        content-align: left middle;
    }

    LiveCockpitApp.short #brand {
        height: 3;
        padding: 0 1;
        content-align: left middle;
    }

    LiveCockpitApp.short #metrics {
        height: 1;
        padding: 0 1;
        content-align: left middle;
    }

    LiveCockpitApp.short #workspace {
        height: 2fr;
        min-height: 6;
    }

    LiveCockpitApp.short #conversation {
        height: 3fr;
        min-height: 7;
    }

    LiveCockpitApp.short #stream-title {
        display: none;
    }

    LiveCockpitApp.short #context-panel {
        width: 1fr;
        min-width: 0;
        margin-right: 0;
    }

    LiveCockpitApp.short #security-panel {
        width: 1fr;
        min-width: 0;
        margin-right: 0;
    }

    LiveCockpitApp.short #time-machine-panel {
        width: 1fr;
        min-width: 0;
        margin-right: 0;
    }

    LiveCockpitApp.short #context-detail-panel {
        display: none;
    }

    LiveCockpitApp.short #security-detail-panel {
        display: none;
    }

    LiveCockpitApp.short #time-machine-detail-panel {
        display: none;
    }

    LiveCockpitApp.short #context-title {
        display: none;
    }

    LiveCockpitApp.short #security-title {
        display: none;
    }

    LiveCockpitApp.short #time-machine-title {
        display: none;
    }

    LiveCockpitApp.short #context-layers {
        height: 1fr;
        padding: 0 1;
    }

    LiveCockpitApp.short #security-capabilities {
        height: 1fr;
        padding: 0 1;
    }

    LiveCockpitApp.short #approval-flows {
        display: none;
    }

    LiveCockpitApp.short #security-watch {
        display: none;
    }

    LiveCockpitApp.short #context-insights {
        display: none;
    }

    LiveCockpitApp.short #time-machine-inline-detail {
        display: block;
        max-height: 5;
        padding: 0 1;
    }

    LiveCockpitApp.output-expanded #workspace {
        display: none;
    }

    LiveCockpitApp.output-expanded #conversation {
        height: 1fr;
        min-height: 0;
        border: round #00d4c7;
    }
    """

    def __init__(
        self,
        agent: LiveAgent,
        event_bus: EventBus,
        *,
        model: str,
        workspace: str,
        security: SecurityShield | None = None,
        security_observer: Callable[[], SecurityShield] | None = None,
        initial_events: Iterable[RuntimeEvent] = (),
        historical_events: Iterable[RuntimeEvent] = (),
        time_machine_history_provider: Callable[[], TimeMachineHistory] | None = None,
        time_machine_persistence_enabled: bool = False,
        persistent_event_count: int = 0,
        filesystem_tools: FileSystemTools | None = None,
        max_events: int = MAX_LIVE_EVENTS,
    ) -> None:
        if max_events < 1:
            raise ValueError("live cockpit event capacity must be at least 1")
        if security_observer is not None and not callable(security_observer):
            raise ValueError("live cockpit security observer must be callable")
        if time_machine_history_provider is not None and not callable(
            time_machine_history_provider
        ):
            raise ValueError("time machine history provider must be callable")
        if type(time_machine_persistence_enabled) is not bool:
            raise ValueError("time machine persistence flag must be boolean")
        if type(persistent_event_count) is not int or persistent_event_count < 0:
            raise ValueError("persistent event count cannot be negative")
        if filesystem_tools is not None and not isinstance(
            filesystem_tools, FileSystemTools
        ):
            raise ValueError("time machine restore requires FileSystemTools")
        super().__init__()
        self._agent = agent
        self._event_bus = event_bus
        self._model = _safe_inline(model)
        self._workspace = _safe_inline(workspace)
        self._security = security or project_security_shield(
            {},
            sandbox_backend="disabled",
            audit_enabled=False,
        )
        self._security_observer = security_observer
        self._security_observations = [self._security]
        self._security_observation_failures = 0
        self._boundary_watch = project_security_boundary_watch(
            self._security_observations
        )
        self._historical_events = tuple(historical_events)
        if any(
            not isinstance(event, RuntimeEvent) for event in self._historical_events
        ):
            raise ValueError("historical events must contain only RuntimeEvent values")
        self._time_machine_history_provider = time_machine_history_provider
        self._filesystem_tools = filesystem_tools
        self._time_machine_history = TimeMachineHistoryProjection()
        self._time_machine_history_failures = 0
        self._time_machine_persistence_enabled = time_machine_persistence_enabled
        self._persistent_event_count = persistent_event_count
        self._time_machine_projector = TimeMachineProjector()
        self._time_machine_selection: TimeMachineSelection | None = None
        materialized_events = tuple(initial_events)
        self._events = list(materialized_events[-max_events:])
        self._max_events = max_events
        self._view_dropped_events = max(
            len(materialized_events) - max_events,
            0,
        )
        self._graph = ExecutionGraphProjector().project(self._events)
        self._metrics = MetricsProjector().project(self._graph)
        self._approval_flow = ApprovalFlowProjector().project(self._graph)
        self._time_machine_snapshot = self._time_machine_projector.project(
            (*self._historical_events, *self._events),
            self._time_machine_history,
            persistence_enabled=self._time_machine_persistence_enabled,
            persistent_event_count=self._persistent_event_count,
        )
        self._context_snapshot = agent.context_tomography()
        self._context_simulation: ContextWhatIf | None = None
        self._monitor_view: MonitorView = "execution"
        self._primary_monitor_view: PrimaryMonitorView = "execution"
        self._filter: NodeFilter = "all"
        self._selected_correlation_id: str | None = None
        self._subscription: EventSubscription | None = None
        self._metrics_timer: Timer | None = None
        self._bridge = RuntimeEventBridge(
            lambda: self.post_message(RuntimeEventsReady())
        )
        self._cancel_requested = Event()
        self._turn_done = Event()
        self._turn_done.set()
        self._busy = False
        self._closed = False
        self._pending_approvals: list[_ApprovalDecision] = []
        self.completed_turns = 0

    @property
    def graph(self) -> ExecutionGraph:
        return self._graph

    @property
    def metrics(self) -> RuntimeMetrics:
        return self._metrics

    @property
    def node_filter(self) -> NodeFilter:
        return self._filter

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def output_expanded(self) -> bool:
        return self.has_class("output-expanded")

    @property
    def monitor_view(self) -> MonitorView:
        return self._monitor_view

    @property
    def context_snapshot(self) -> ContextTomography:
        return self._context_snapshot

    @property
    def context_simulation(self) -> ContextWhatIf | None:
        return self._context_simulation

    @property
    def security_snapshot(self) -> SecurityShield:
        return self._security

    @property
    def approval_flow(self) -> ApprovalFlow:
        return self._approval_flow

    @property
    def boundary_watch(self) -> SecurityBoundaryWatch:
        return self._boundary_watch

    @property
    def time_machine_snapshot(self) -> TimeMachineSnapshot:
        return self._time_machine_snapshot

    @property
    def _cockpit_screen(self) -> Screen[Any]:
        return self.screen_stack[0]

    def compose(self) -> ComposeResult:
        yield Static(self._brand_text(), id="brand")
        yield Static(id="metrics")
        with ContentSwitcher(initial="execution-view", id="workspace"):
            with Horizontal(id="execution-view", classes="workspace-view"):
                with Vertical(id="dag-panel"):
                    yield Static(id="tree-title", classes="panel-title")
                    tree: Tree[str] = Tree("EXECUTION TREE", id="execution-tree")
                    tree.show_guides = True
                    tree.guide_depth = 3
                    yield tree
                with Vertical(id="detail-panel"):
                    yield Static("NODE TELEMETRY", classes="panel-title")
                    yield Static(
                        Text("选择节点以查看安全元数据", style="dim"),
                        id="node-detail",
                    )
            with Horizontal(id="context-view", classes="workspace-view"):
                with Vertical(id="context-panel"):
                    yield Static(
                        "CONTEXT LAYERS  ·  LOCAL ESTIMATE",
                        id="context-title",
                        classes="panel-title",
                    )
                    yield Static(id="context-layers")
                    yield Static(id="context-insights")
                with Vertical(id="context-detail-panel"):
                    yield Static("LOCAL / SERVER TELEMETRY", classes="panel-title")
                    yield Static(id="context-detail")
            with Horizontal(id="security-view", classes="workspace-view"):
                with Vertical(id="security-panel"):
                    yield Static(
                        "CAPABILITY BANDS  ·  APPLICATION + OS",
                        id="security-title",
                        classes="panel-title",
                    )
                    yield Static(id="security-capabilities")
                    yield Static(id="security-watch")
                    yield Static(id="approval-flows")
                with Vertical(id="security-detail-panel"):
                    yield Static("ENFORCEMENT LAYERS", classes="panel-title")
                    yield Static(id="security-detail")
            with Horizontal(id="time-machine-view", classes="workspace-view"):
                with Vertical(id="time-machine-panel"):
                    yield Static(
                        "TIME MACHINE  ·  READ ONLY  ·  METADATA ONLY",
                        id="time-machine-title",
                        classes="panel-title",
                    )
                    history_tree: Tree[TimeMachineSelection] = Tree(
                        "HISTORY",
                        id="time-machine-tree",
                    )
                    history_tree.show_guides = True
                    history_tree.guide_depth = 2
                    yield history_tree
                    yield Static(id="time-machine-inline-detail")
                with Vertical(id="time-machine-detail-panel"):
                    yield Static("HISTORICAL PROJECTION", classes="panel-title")
                    yield Static(id="time-machine-detail")
        with Vertical(id="conversation"):
            yield Static(
                self._stream_title_text(),
                id="stream-title",
            )
            yield Log(
                id="transcript",
                auto_scroll=True,
                max_lines=MAX_LIVE_OUTPUT_LINES,
            )
            yield Input(
                placeholder="向 Neil Agent 提交任务，Enter 发送",
                id="prompt",
            )
        yield Footer()

    def on_mount(self) -> None:
        self._sync_responsive_classes(self.size.width, self.size.height)
        self._subscription = self._event_bus.subscribe(self._bridge.observe)
        self._refresh_projection()
        self._refresh_context_view()
        self._refresh_security_view()
        self._refresh_time_machine_view()
        self._metrics_timer = self.set_interval(0.25, self._refresh_metrics)
        self._cockpit_screen.query_one("#prompt", Input).focus()

    def on_unmount(self) -> None:
        self._closed = True
        self._cancel_requested.set()
        metrics_timer = self._metrics_timer
        self._metrics_timer = None
        if metrics_timer is not None:
            metrics_timer.stop()
        subscription = self._subscription
        self._subscription = None
        if subscription is not None:
            subscription.close()
        self._bridge.close()
        self._reject_pending_approvals()

    def on_resize(self, event: events.Resize) -> None:
        previous_density = (
            self.has_class("narrow"),
            self.has_class("short"),
        )
        self._sync_responsive_classes(event.size.width, event.size.height)
        self._refresh_metrics()
        current_density = (
            self.has_class("narrow"),
            self.has_class("short"),
        )
        if previous_density != current_density:
            self._refresh_context_view()
            self._refresh_security_view()
            self._refresh_time_machine_view()

    @on(Input.Submitted, "#prompt")
    def submit_prompt(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if not prompt:
            return
        if self._busy:
            self.notify("当前请求仍在执行", severity="warning")
            return
        event.input.value = ""
        event.input.disabled = True
        self._busy = True
        self.refresh_bindings()
        self._cancel_requested.clear()
        self._turn_done.clear()
        self._context_simulation = None
        self._context_snapshot = self._agent.context_tomography(prompt)
        self._refresh_context_view()
        transcript = self._cockpit_screen.query_one("#transcript", Log)
        transcript.write_line(f"YOU  ›  {prompt}")
        transcript.write("NEIL ›  ")
        self.run_worker(
            lambda: self._run_agent_turn(prompt),
            name="live-agent-turn",
            group="agent-turn",
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )
        self._refresh_metrics()

    def on_runtime_events_ready(self) -> None:
        incoming = self._bridge.drain()
        if not incoming:
            return
        overflow = max(len(self._events) + len(incoming) - self._max_events, 0)
        if overflow:
            del self._events[:overflow]
            self._view_dropped_events += overflow
        self._events.extend(incoming)
        self._refresh_projection()
        if self._monitor_view == "time-machine":
            self._refresh_time_machine_snapshot(refresh_history=False)

    def on_assistant_chunk(self, message: AssistantChunk) -> None:
        self._cockpit_screen.query_one("#transcript", Log).write(
            _safe_multiline(message.chunk)
        )

    def on_turn_finished(self, message: TurnFinished) -> None:
        self._busy = False
        self.refresh_bindings()
        if message.succeeded:
            self.completed_turns += 1
            summary = Text("✓ 请求完成", style="green")
        elif message.cancelled:
            summary = Text("■ 请求已取消", style="yellow")
        else:
            summary = Text("▲ 请求失败", style="bold red")
        transcript = self._cockpit_screen.query_one("#transcript", Log)
        transcript.write_line(f"\n{summary.plain}")
        prompt = self._cockpit_screen.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.focus()
        self._context_simulation = None
        self._context_snapshot = self._agent.context_tomography()
        self._refresh_context_view()
        if self._monitor_view == "time-machine":
            self._refresh_time_machine_snapshot(refresh_history=True)
        self._turn_done.set()
        self._refresh_metrics()

    def on_approval_requested(self, message: ApprovalRequested) -> None:
        if self._closed or self._cancel_requested.is_set():
            message.decision.ready.set()
            return
        self._pending_approvals.append(message.decision)
        self.push_screen(
            ToolApprovalScreen(message.tool_name, message.preview),
            lambda approved: self._resolve_approval(
                message.decision,
                bool(approved),
            ),
        )

    @on(Tree.NodeHighlighted, "#execution-tree")
    def highlight_node(self, message: Tree.NodeHighlighted[str]) -> None:
        correlation_id = message.node.data
        if correlation_id is None:
            return
        self._selected_correlation_id = correlation_id
        self._refresh_detail()

    @on(Tree.NodeHighlighted, "#time-machine-tree")
    def highlight_time_machine_point(
        self,
        message: Tree.NodeHighlighted[TimeMachineSelection],
    ) -> None:
        selection = message.node.data
        if selection is None:
            return
        self._time_machine_selection = selection
        if selection.kind == "event":
            entry = next(
                (
                    item
                    for item in self._time_machine_snapshot.timeline.entries
                    if item.event_id == selection.key
                ),
                None,
            )
            if entry is not None:
                self._time_machine_snapshot = self._project_time_machine(
                    cursor_sequence=entry.sequence
                )
                tree = self._cockpit_screen.query_one("#time-machine-tree", Tree)
                tree.root.set_label(self._time_machine_root_label())
        self._refresh_time_machine_detail()

    def request_tool_approval(self, call: ToolCall, preview: str) -> bool:
        """Block only the Agent worker while the UI obtains a decision."""

        if self._closed or self._cancel_requested.is_set():
            return False
        decision = _ApprovalDecision()
        self.post_message(
            ApprovalRequested(
                tool_name=call.name,
                preview=preview,
                decision=decision,
            )
        )
        decision.ready.wait()
        return decision.approved

    def wait_until_idle(self, timeout: float) -> bool:
        return self._turn_done.wait(timeout)

    def action_request_exit(self) -> None:
        if self._busy:
            self.notify(
                "请求执行中；先按 Ctrl+X 取消，完成后再退出",
                severity="warning",
            )
            return
        self.exit()

    async def action_quit(self) -> None:
        self.action_request_exit()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action.startswith("filter_") and self._monitor_view != "execution":
            return False
        if action == "context_what_if" and (
            self._monitor_view != "context" or self._busy
        ):
            return False
        return super().check_action(action, parameters)

    def action_cancel_turn(self) -> None:
        if not self._busy:
            self.notify("当前没有正在执行的请求")
            return
        self._cancel_requested.set()
        self._reject_pending_approvals()
        self.notify("已请求取消；正在等待当前操作返回", severity="warning")
        self._refresh_metrics()

    def action_toggle_output(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.set_class(not self.output_expanded, "output-expanded")
        self._refresh_stream_title()
        self._cockpit_screen.query_one("#prompt", Input).focus()

    def action_toggle_monitor(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self._primary_monitor_view = (
            "context" if self._primary_monitor_view == "execution" else "execution"
        )
        self._set_monitor_view(self._primary_monitor_view)

    def action_toggle_security(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        target: MonitorView = (
            self._primary_monitor_view
            if self._monitor_view == "security"
            else "security"
        )
        self._set_monitor_view(target)

    def action_toggle_time_machine(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        target: MonitorView = (
            self._primary_monitor_view
            if self._monitor_view == "time-machine"
            else "time-machine"
        )
        self._set_monitor_view(target)

    def action_restore_checkpoint(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if self._monitor_view != "time-machine":
            return
        if not can_offer_checkpoint_restore(
            self._time_machine_snapshot,
            self._time_machine_selection,
            busy=self._busy,
            pending_approvals=len(self._pending_approvals),
            restore_available=self._filesystem_tools is not None,
        ):
            self.notify("当前无法恢复任务检查点", severity="warning")
            return
        selection = self._time_machine_selection
        tools = self._filesystem_tools
        if selection is None or tools is None:
            return
        try:
            prepared = tools.prepare_checkpoint_restore(selection.key)
        except ToolError as error:
            self.notify(str(error), severity="error")
            return
        self.push_screen(
            CheckpointRestoreScreen(prepared.checkpoint_id, prepared.preview),
            lambda approved: self._finish_checkpoint_restore(approved, prepared),
        )

    def _finish_checkpoint_restore(
        self,
        approved: bool,
        prepared: PreparedFileRestore,
    ) -> None:
        if not approved:
            self.notify("已取消任务检查点恢复", severity="warning")
            return
        tools = self._filesystem_tools
        if tools is None:
            return
        try:
            result = tools.apply_latest_restore(prepared)
        except ToolError as error:
            self.notify(str(error), severity="error")
            return
        self.notify(result, severity="information")
        self._refresh_time_machine_snapshot(refresh_history=True)

    def action_context_what_if(self) -> None:
        if (
            isinstance(self.screen, ModalScreen)
            or self._monitor_view != "context"
            or self._busy
        ):
            return
        initial_chars = (
            self._context_simulation.additional_chars
            if self._context_simulation is not None
            else 10_000
        )
        self.push_screen(
            ContextWhatIfScreen(initial_chars),
            self._apply_context_what_if,
        )

    def action_filter_all(self) -> None:
        self._set_filter("all")

    def action_filter_active(self) -> None:
        self._set_filter("active")

    def action_filter_failed(self) -> None:
        self._set_filter("failed")

    def action_filter_tools(self) -> None:
        self._set_filter("tools")

    def _apply_context_what_if(self, additional_chars: int | None) -> None:
        if additional_chars is None:
            return
        if additional_chars == 0:
            self._context_simulation = None
            self.notify("已清除本地上下文模拟")
        else:
            try:
                self._context_simulation = self._agent.context_what_if(additional_chars)
            except ValueError as error:
                self.notify(_safe_inline(str(error)), severity="warning")
                return
            self.notify(
                f"已模拟增加 {additional_chars:,} 字符；未调用模型",
                severity="information",
            )
        self._refresh_context_view()
        self.refresh_bindings()
        self._cockpit_screen.query_one("#prompt", Input).focus()

    def _run_agent_turn(self, prompt: str) -> None:
        cancelled = False
        succeeded = False
        try:
            stream = self._agent.stream_chat(prompt)
            for chunk in stream:
                if self._cancel_requested.is_set():
                    cancelled = True
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
                    break
                self.post_message(AssistantChunk(chunk))
            else:
                succeeded = True
        except NeilAgentError as error:
            self.post_message(
                AssistantChunk(
                    "\n"
                    + _safe_inline(
                        f"请求失败：{error}",
                        max_chars=MAX_LIVE_ERROR_CHARS,
                    )
                )
            )
        except Exception as error:  # noqa: BLE001 - UI degradation boundary.
            self.post_message(
                AssistantChunk(
                    "\n"
                    + _safe_inline(
                        f"实时模式发生 {type(error).__name__}",
                        max_chars=MAX_LIVE_ERROR_CHARS,
                    )
                )
            )
        finally:
            cancelled = cancelled or self._cancel_requested.is_set()
            self._event_bus.flush(0.5)
            self.post_message(
                TurnFinished(
                    succeeded=succeeded and not cancelled,
                    cancelled=cancelled,
                )
            )

    def _set_filter(self, node_filter: NodeFilter) -> None:
        if node_filter == self._filter:
            return
        self._filter = node_filter
        self._refresh_tree()
        self._refresh_metrics()

    def _set_monitor_view(self, monitor_view: MonitorView) -> None:
        self._monitor_view = monitor_view
        self._cockpit_screen.query_one(
            "#workspace", ContentSwitcher
        ).current = f"{monitor_view}-view"
        if monitor_view == "context":
            self._refresh_context_view()
        elif monitor_view == "security":
            self._observe_security_boundaries()
            self._refresh_security_view()
        elif monitor_view == "time-machine":
            self._refresh_time_machine_snapshot(refresh_history=True)
        self._refresh_stream_title()
        self.refresh_bindings()
        self._cockpit_screen.query_one("#prompt", Input).focus()

    def _observe_security_boundaries(self) -> None:
        if self._security_observer is None:
            return
        try:
            observation = self._security_observer()
            if not isinstance(observation, SecurityShield):
                raise ValueError("security observer returned an invalid snapshot")
        except Exception:  # noqa: BLE001 - optional observation degradation boundary.
            self._security_observation_failures += 1
            self.notify(
                "安全边界观察失败；已保留上一份安全快照",
                severity="error",
            )
        else:
            self._security = observation
            self._security_observations.append(observation)
            if len(self._security_observations) > MAX_SECURITY_OBSERVATIONS:
                del self._security_observations[0]
        self._boundary_watch = project_security_boundary_watch(
            self._security_observations,
            observation_failures=self._security_observation_failures,
        )

    def _sync_responsive_classes(self, width: int, height: int) -> None:
        self.set_class(width < NARROW_TERMINAL_WIDTH, "narrow")
        self.set_class(height < SHORT_TERMINAL_HEIGHT, "short")

    def _stream_title_text(self) -> str:
        monitor_label = {
            "execution": "执行树",
            "context": "上下文",
            "security": "安全盾",
            "time-machine": "时间机器",
        }[self._monitor_view]
        action = f"F2 返回{monitor_label}" if self.output_expanded else "F2 展开结果"
        return f"AGENT STREAM  ·  {action}  ·  最近 {MAX_LIVE_OUTPUT_LINES} 行"

    def _refresh_stream_title(self) -> None:
        self._cockpit_screen.query_one("#stream-title", Static).update(
            self._stream_title_text()
        )

    def _refresh_context_view(self) -> None:
        compact = self.has_class("narrow") or self.has_class("short")
        pressure = context_budget_pressure(self._context_snapshot)
        pressure_label = _CONTEXT_PRESSURE_LABELS[pressure.level]
        context_panel = self._cockpit_screen.query_one("#context-panel", Vertical)
        visible_pressure = (
            self._context_simulation.projected_pressure
            if self._context_simulation is not None
            else pressure
        )
        context_panel.styles.border = (
            "round",
            _CONTEXT_PRESSURE_BORDER_COLORS[visible_pressure.level],
        )
        context_panel.border_title = (
            f" CONTEXT · BASE {pressure_label} {_pressure_percent(pressure):.1f}% · "
            f"{self._context_snapshot.estimated_chars:,}c / "
            f"~{self._context_snapshot.estimated_tokens:,}t "
            if self.has_class("short")
            else None
        )
        context_panel.border_subtitle = (
            f" {_compact_context_signal(self._context_snapshot, self._context_simulation)} "
            if self.has_class("short")
            else None
        )
        title = (
            "CONTEXT  ·  LOCAL ESTIMATE"
            if compact
            else "CONTEXT LAYERS  ·  LOCAL ESTIMATE  ·  METADATA ONLY"
        )
        title_text = Text(f"{title}  ·  ")
        title_text.append(
            f"{pressure_label} {_pressure_percent(pressure):.1f}%",
            style=_CONTEXT_PRESSURE_STYLES[pressure.level],
        )
        title_text.append(
            f"  ·  {self._context_snapshot.estimated_chars:,}c / "
            f"~{self._context_snapshot.estimated_tokens:,}t"
        )
        self._cockpit_screen.query_one("#context-title", Static).update(title_text)
        self._cockpit_screen.query_one("#context-layers", Static).update(
            format_context_layers(self._context_snapshot, compact=compact)
        )
        self._cockpit_screen.query_one("#context-insights", Static).update(
            format_context_insights(
                self._context_snapshot,
                self._context_simulation,
                compact=compact,
            )
        )
        self._cockpit_screen.query_one("#context-detail", Static).update(
            format_context_detail(
                self._context_snapshot,
                self._context_simulation,
            )
        )

    def _refresh_security_view(self) -> None:
        compact = self.has_class("narrow") or self.has_class("short")
        panel = self._cockpit_screen.query_one("#security-panel", Vertical)
        os_boundary = self._security.os_sandbox
        has_critical_alert = any(
            alert.severity == "critical" for alert in self._boundary_watch.alerts
        )
        has_warning_alert = any(
            alert.severity == "warning" for alert in self._boundary_watch.alerts
        )
        panel.styles.border = (
            "round",
            "#d72d5b"
            if has_critical_alert
            else "#7d5b24"
            if has_warning_alert
            else "#277c6f"
            if os_boundary.status == "ready"
            else "#d72d5b"
            if os_boundary.status in {"incomplete", "unavailable"}
            else "#7d5b24",
        )
        panel.border_title = (
            f" APP {self._security.application.status.upper()} · "
            f"{_compact_approval_signal(self._approval_flow)} "
            if self.has_class("short")
            else None
        )
        panel.border_subtitle = (
            f" {_compact_boundary_signal(self._boundary_watch)} "
            if self.has_class("short")
            else None
        )
        self._cockpit_screen.query_one("#security-title", Static).update(
            format_security_title(
                self._security,
                compact=compact,
                boundary_watch=self._boundary_watch,
            )
        )
        self._cockpit_screen.query_one("#security-capabilities", Static).update(
            format_security_capabilities(
                self._security,
                compact=compact,
                short=self.has_class("short"),
            )
        )
        self._cockpit_screen.query_one("#security-watch", Static).update(
            format_security_boundary_watch(self._boundary_watch, compact=True)
        )
        self._cockpit_screen.query_one("#approval-flows", Static).update(
            format_approval_flows(
                self._approval_flow,
                compact=compact,
                limit=1 if compact else 4,
            )
        )
        self._cockpit_screen.query_one("#security-detail", Static).update(
            format_security_boundaries(self._security, self._boundary_watch)
        )

    def _project_time_machine(
        self,
        *,
        cursor_sequence: int | None = None,
    ) -> TimeMachineSnapshot:
        return self._time_machine_projector.project(
            (*self._historical_events, *self._events),
            self._time_machine_history,
            cursor_sequence=cursor_sequence,
            persistence_enabled=self._time_machine_persistence_enabled,
            persistent_event_count=self._persistent_event_count,
        )

    def _refresh_time_machine_snapshot(self, *, refresh_history: bool) -> None:
        provider = self._time_machine_history_provider
        if refresh_history and provider is not None:
            try:
                history = provider()
                if not isinstance(history, TimeMachineHistory):
                    raise ValueError("time machine provider returned invalid history")
            except Exception:  # noqa: BLE001 - read-only observation boundary.
                self._time_machine_history_failures += 1
                self.notify(
                    "时间机器历史读取失败；已保留上一份脱敏历史快照",
                    severity="error",
                )
            else:
                self._time_machine_history = (
                    self._time_machine_projector.sanitize_history(history)
                )
        self._time_machine_snapshot = self._project_time_machine()
        self._refresh_time_machine_view()

    def _refresh_time_machine_view(self) -> None:
        snapshot = self._time_machine_snapshot
        panel = self._cockpit_screen.query_one("#time-machine-panel", Vertical)
        storage = (
            f"STORE ON · LOADED {snapshot.persistent_event_count}"
            if snapshot.persistence_enabled
            else "MEMORY ONLY"
        )
        panel.styles.border = (
            "round",
            "#d72d5b" if self._time_machine_history_failures else "#356c94",
        )
        panel.border_title = (
            f" TIME MACHINE · {storage} · E{len(snapshot.timeline.entries)} "
            if self.has_class("short")
            else None
        )
        panel.border_subtitle = (
            f" S{len(snapshot.sessions)} · C{len(snapshot.checkpoints)} · READ ONLY "
            if self.has_class("short")
            else None
        )
        title = Text("TIME MACHINE  ·  READ ONLY  ·  ", style="bold #9bcdf5")
        title.append(
            storage, style="#9ee37d" if snapshot.persistence_enabled else "dim"
        )
        title.append(
            f"  ·  E{len(snapshot.timeline.entries)} "
            f"S{len(snapshot.sessions)} C{len(snapshot.checkpoints)}",
            style="dim",
        )
        if self._time_machine_history_failures:
            title.append(
                f"  ·  SOURCE FAIL {self._time_machine_history_failures}",
                style="bold red",
            )
        self._cockpit_screen.query_one("#time-machine-title", Static).update(title)
        self._refresh_time_machine_tree()

    def _refresh_time_machine_tree(self) -> None:
        snapshot = self._time_machine_snapshot
        tree = self._cockpit_screen.query_one(
            "#time-machine-tree",
            Tree,
        )
        tree.reset(self._time_machine_root_label())
        tree.root.expand()
        nodes: dict[TimeMachineSelection, TreeNode[TimeMachineSelection]] = {}

        event_root = tree.root.add(
            Text(
                f"RUNTIME EVENTS  {len(snapshot.timeline.entries)}",
                style="bold #91f5e9",
            ),
            expand=True,
        )
        for entry in snapshot.timeline.entries:
            selection = TimeMachineSelection("event", entry.event_id)
            label = Text(f"{entry.sequence:04d} ", style="dim")
            label.append(_time_label(entry.timestamp), style="#8fa9bd")
            label.append(f"  {entry.stage.upper():<13}")
            label.append(
                f" {_STATUS_MARKER[entry.status]} {entry.status.upper()}",
                style=_STATUS_STYLE[entry.status],
            )
            nodes[selection] = event_root.add_leaf(label, data=selection)

        session_root = tree.root.add(
            Text(
                f"CURRENT SESSION CATALOG  {len(snapshot.sessions)}",
                style="bold #c8b8ff",
            ),
            expand=True,
        )
        for session_point in snapshot.sessions:
            selection = TimeMachineSelection("session", session_point.session_id)
            lineage = {
                "root": "ROOT",
                "branch": "BRANCH",
                "orphaned_branch": "ORPHAN",
            }[session_point.lineage]
            label = Text(f"{lineage:<6} {session_point.session_id[-8:]}")
            label.append(f"  {session_point.round_count}R", style="dim")
            if session_point.has_compaction:
                label.append("  COMPACT", style="bold #ffb454")
            if session_point.failed_check:
                label.append("  FAILED", style="bold red")
            nodes[selection] = session_root.add_leaf(label, data=selection)

        checkpoint_root = tree.root.add(
            Text(
                f"CURRENT TASK CHECKPOINTS  {len(snapshot.checkpoints)}",
                style="bold #ffb454",
            ),
            expand=True,
        )
        for checkpoint_point in snapshot.checkpoints:
            selection = TimeMachineSelection(
                "checkpoint", checkpoint_point.checkpoint_id
            )
            label = Text(f"{checkpoint_point.checkpoint_id[:12]:<12}")
            label.append(
                f"  {checkpoint_point.file_count} FILES  "
                f"{checkpoint_point.resulting_chars} CHARS",
                style="dim",
            )
            nodes[selection] = checkpoint_root.add_leaf(label, data=selection)

        if not nodes:
            tree.root.add_leaf(Text("尚无可回放的元数据", style="dim"))
            self._time_machine_selection = None
        elif self._time_machine_selection not in nodes:
            if snapshot.timeline.entries:
                selected_event = snapshot.timeline.entries[-1]
                self._time_machine_selection = TimeMachineSelection(
                    "event",
                    selected_event.event_id,
                )
            elif snapshot.sessions:
                self._time_machine_selection = TimeMachineSelection(
                    "session",
                    snapshot.sessions[-1].session_id,
                )
            else:
                self._time_machine_selection = TimeMachineSelection(
                    "checkpoint",
                    snapshot.checkpoints[-1].checkpoint_id,
                )

        selected = (
            None
            if self._time_machine_selection is None
            else nodes.get(self._time_machine_selection)
        )
        if selected is not None:
            active_selection = self._time_machine_selection
            if active_selection is not None and active_selection.kind == "event":
                selected_entry = next(
                    (
                        timeline_entry
                        for timeline_entry in snapshot.timeline.entries
                        if timeline_entry.event_id == active_selection.key
                    ),
                    None,
                )
                if (
                    selected_entry is not None
                    and selected_entry.sequence != snapshot.cursor_sequence
                ):
                    self._time_machine_snapshot = self._project_time_machine(
                        cursor_sequence=selected_entry.sequence
                    )
                    tree.root.set_label(self._time_machine_root_label())
            tree.select_node(selected)
        self._refresh_time_machine_detail()

    def _time_machine_root_label(self) -> Text:
        snapshot = self._time_machine_snapshot
        return Text(
            f"HISTORY · {snapshot.cursor_sequence}/{len(snapshot.timeline.entries)}",
            style="bold #9bcdf5",
        )

    def _refresh_time_machine_detail(self) -> None:
        rendered = render_time_machine_snapshot(
            self._time_machine_snapshot,
            self._time_machine_selection,
        )
        hint = checkpoint_restore_hint(
            self._time_machine_snapshot,
            self._time_machine_selection,
            busy=self._busy,
            pending_approvals=len(self._pending_approvals),
            restore_available=self._filesystem_tools is not None,
        )
        if hint is not None:
            rendered = f"{rendered}\n\n{hint}"
        content = Text(rendered)
        self._cockpit_screen.query_one("#time-machine-detail", Static).update(content)
        self._cockpit_screen.query_one(
            "#time-machine-inline-detail",
            Static,
        ).update(content)

    def _refresh_projection(self) -> None:
        self._graph = ExecutionGraphProjector().project(self._events)
        self._metrics = MetricsProjector().project(self._graph)
        self._approval_flow = ApprovalFlowProjector().project(self._graph)
        self._refresh_tree()
        self._refresh_security_view()
        self._refresh_metrics()

    def _refresh_tree(self) -> None:
        tree = self._cockpit_screen.query_one("#execution-tree", Tree)
        visible = visible_node_ids(self._graph, self._filter)
        tree.reset(
            Text(
                f"EXECUTION · {len(visible)}/{len(self._graph.nodes)}",
                style="bold #91f5e9",
            )
        )
        tree.root.expand()
        nodes = {
            node.correlation_id: node
            for node in self._graph.nodes
            if node.correlation_id in visible
        }
        children: dict[str | None, list[ExecutionNode]] = defaultdict(list)
        for node in nodes.values():
            parent = (
                node.parent_correlation_id
                if node.parent_correlation_id in nodes
                else None
            )
            children[parent].append(node)
        order = {
            node.correlation_id: index for index, node in enumerate(self._graph.nodes)
        }
        for siblings in children.values():
            siblings.sort(key=lambda node: order[node.correlation_id])

        added: dict[str, TreeNode[str]] = {}
        stack = [(tree.root, node) for node in reversed(children.get(None, ()))]
        while stack:
            parent_tree_node, node = stack.pop()
            tree_node = parent_tree_node.add(
                format_node_label(
                    node,
                    approval_trace=self._approval_flow.trace(node.correlation_id),
                ),
                data=node.correlation_id,
                expand=True,
            )
            added[node.correlation_id] = tree_node
            stack.extend(
                (tree_node, child)
                for child in reversed(children.get(node.correlation_id, ()))
            )

        title = self._cockpit_screen.query_one("#tree-title", Static)
        title.update(
            Text(
                f"LIVE EXECUTION TREE  ·  FILTER {_FILTER_LABELS[self._filter]}",
                style="bold #91f5e9",
            )
        )
        if not nodes:
            tree.root.add_leaf(Text("没有匹配的执行节点", style="dim"))
            self._selected_correlation_id = None
            self._cockpit_screen.query_one("#node-detail", Static).update(
                Text("调整筛选条件或提交一个任务", style="dim")
            )
            return

        if self._selected_correlation_id not in nodes:
            self._selected_correlation_id = next(reversed(nodes))
        selected = added.get(self._selected_correlation_id)
        if selected is not None:
            tree.select_node(selected)
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        detail = self._cockpit_screen.query_one("#node-detail", Static)
        if self._selected_correlation_id is None:
            detail.update(Text("选择节点以查看安全元数据", style="dim"))
            return
        node = self._graph.node(self._selected_correlation_id)
        if node is None:
            detail.update(Text("节点已不在当前有界事件窗口中", style="dim"))
            return
        detail.update(
            format_node_detail(
                node,
                approval_trace=self._approval_flow.trace(node.correlation_id),
            )
        )

    def _refresh_metrics(self) -> None:
        if self._closed:
            return
        try:
            metrics = self._cockpit_screen.query_one("#metrics", Static)
        except NoMatches:
            # A final timer tick may overlap Textual's child-unmount phase.
            return
        metrics.update(
            format_metrics(
                self._metrics,
                filter_name=_FILTER_LABELS[self._filter],
                bus_dropped=self._event_bus.stats.dropped_deliveries,
                view_dropped=(self._view_dropped_events + self._bridge.dropped_events),
                busy=self._busy,
                cancelling=self._cancel_requested.is_set() and self._busy,
                compact=self.has_class("narrow") or self.has_class("short"),
            )
        )

    def _brand_text(self) -> Text:
        brand = Text("NEIL // LIVE MISSION CONTROL", style="bold #f4f7fb")
        brand.append("   ")
        brand.append(self._model, style="#91f5e9")
        brand.append("   ")
        brand.append(self._workspace, style="dim")
        return brand

    def _resolve_approval(
        self,
        decision: _ApprovalDecision,
        approved: bool,
    ) -> None:
        if decision.ready.is_set():
            return
        decision.approved = approved
        decision.ready.set()
        if decision in self._pending_approvals:
            self._pending_approvals.remove(decision)

    def _reject_pending_approvals(self) -> None:
        for decision in tuple(self._pending_approvals):
            self._resolve_approval(decision, False)


def run_live_cockpit(
    agent: LiveAgent,
    event_bus: EventBus,
    *,
    model: str,
    workspace: str,
    security: SecurityShield | None = None,
    security_observer: Callable[[], SecurityShield] | None = None,
    historical_events: Iterable[RuntimeEvent] = (),
    time_machine_history_provider: Callable[[], TimeMachineHistory] | None = None,
    time_machine_persistence_enabled: bool = False,
    persistent_event_count: int = 0,
    filesystem_tools: FileSystemTools | None = None,
    approval_handler_owner: object | None = None,
) -> int:
    """Run the app and return the number of successful turns it completed."""

    app = LiveCockpitApp(
        agent,
        event_bus,
        model=model,
        workspace=workspace,
        security=security,
        security_observer=security_observer,
        historical_events=historical_events,
        time_machine_history_provider=time_machine_history_provider,
        time_machine_persistence_enabled=time_machine_persistence_enabled,
        persistent_event_count=persistent_event_count,
        filesystem_tools=filesystem_tools,
    )
    previous_handler: object | None = None
    replace_handler = getattr(
        approval_handler_owner,
        "replace_approval_handler",
        None,
    )
    if callable(replace_handler):
        previous_handler = replace_handler(app.request_tool_approval)
    try:
        app.run(mouse=True)
        app.wait_until_idle(2)
        return app.completed_turns
    finally:
        if callable(replace_handler):
            replace_handler(previous_handler)


def format_security_title(
    security: SecurityShield,
    *,
    compact: bool = False,
    boundary_watch: SecurityBoundaryWatch | None = None,
) -> Text:
    """Render the shared four-state legend without relying on color alone."""

    output = Text("SECURITY SHIELD  ·  " if compact else "CAPABILITY BANDS  ·  ")
    for index, state in enumerate(_SECURITY_STATE_LABELS):
        if index:
            output.append("  ·  ", style="dim")
        output.append(
            f"{_SECURITY_STATE_LABELS[state]} {security.capability_count(state)}",
            style=_SECURITY_STATE_STYLES[state],
        )
    if boundary_watch is not None:
        output.append("  ·  ", style="dim")
        output.append(
            f"ALERT {boundary_watch.warning_count}",
            style=("bold #ff4f7d" if boundary_watch.warning_count else "#9ee37d"),
        )
    return output


def format_security_capabilities(
    security: SecurityShield,
    *,
    compact: bool = False,
    short: bool = False,
) -> Text:
    """Render deterministic bands; capability details never include runtime values."""

    output = Text()
    capabilities = security.capabilities
    if short:
        capabilities = tuple(
            capability
            for capability in capabilities
            if capability.state not in {"forbidden", "unavailable"}
        )
    for index, capability in enumerate(capabilities):
        if index:
            output.append("\n")
        style = _SECURITY_STATE_STYLES[capability.state]
        output.append(
            f"{_SECURITY_STATE_MARKERS[capability.state]} "
            f"{_SECURITY_STATE_LABELS[capability.state]:<11}",
            style=style,
        )
        output.append(f" {capability.label}", style="bold #f4f7fb")
        if capability.tool_count:
            output.append(f"  ×{capability.tool_count}", style="dim")
        if not compact:
            output.append(f"  ·  {capability.summary}", style="dim")
    if short:
        if capabilities:
            output.append("\n")
        output.append("■ FORBIDDEN", style=_SECURITY_STATE_STYLES["forbidden"])
        output.append(" HOST SHELL", style="bold #f4f7fb")
        output.append("  ·  ", style="dim")
        output.append("○ UNAVAILABLE", style=_SECURITY_STATE_STYLES["unavailable"])
        output.append(" OS CMD", style="bold #f4f7fb")
    return output


def format_approval_flows(
    flow: ApprovalFlow,
    *,
    compact: bool = False,
    limit: int = 4,
) -> Text:
    """Render recent approval decisions and preview bindings without previews."""

    if compact:
        output = Text("APPROVAL", style="bold #ffd08a")
        if not flow.traces:
            output.append("  ·  NONE", style="dim")
            return output
        visible = tuple(reversed(flow.traces[-max(limit, 1) :]))
        for index, trace in enumerate(visible):
            style = _approval_trace_style(trace)
            output.append("  ·  " if index == 0 else "\n", style="dim")
            output.append(f"{_approval_trace_marker(trace)} ", style=style)
            output.append(_safe_inline(trace.tool_name), style="bold #f4f7fb")
            decision = _APPROVAL_DECISION_LABELS[trace.decision]
            binding = _PREVIEW_BINDING_LABELS[trace.preview_binding]
            output.append(
                f"  {decision}/{binding.removeprefix('BINDING ')}",
                style=style,
            )
        omitted = len(flow.traces) - len(visible)
        if omitted:
            output.append(f"\n… {omitted} EARLIER", style="dim")
        return output

    output = Text("APPROVAL FLOW", style="bold #ffd08a")
    output.append(f"  ·  {len(flow.traces)}", style="dim")
    output.append("  ·  DAG LINKED", style="#9bcdf5")
    if not compact:
        output.append("  ·  PREVIEW BODY HIDDEN", style="dim")
    if not flow.traces:
        output.append("\n○ NO APPROVAL REQUESTS IN EVENT WINDOW", style="dim")
        return output

    visible = tuple(reversed(flow.traces[-max(limit, 1) :]))
    for trace in visible:
        style = _approval_trace_style(trace)
        output.append("\n")
        output.append(f"{_approval_trace_marker(trace)} ", style=style)
        output.append(_safe_inline(trace.tool_name), style="bold #f4f7fb")
        decision = _APPROVAL_DECISION_LABELS[trace.decision]
        binding = _PREVIEW_BINDING_LABELS[trace.preview_binding]
        output.append(f"  {decision} · {binding}", style=style)
        if trace.tool_correlation_id is None:
            output.append("  → UNRESOLVED TOOL", style="bold red")
        else:
            output.append(f"  → {_short_id(trace.tool_correlation_id)}", style="dim")
        if trace.elapsed_ms is not None:
            output.append(f"  ·  {trace.elapsed_ms:,}ms", style="dim")
    omitted = len(flow.traces) - len(visible)
    if omitted:
        output.append(f"\n… {omitted} EARLIER APPROVALS", style="dim")
    return output


def format_security_boundary_watch(
    watch: SecurityBoundaryWatch,
    *,
    compact: bool = False,
    change_limit: int = 3,
    alert_limit: int = 4,
) -> Text:
    """Render four fixed boundaries, recent changes, and bounded coded alerts."""

    if compact:
        output = Text("BOUNDARY", style="bold #ffd08a")
        for signal in watch.signals:
            output.append(" · ", style="dim")
            output.append(
                _compact_boundary_token(signal), style=_boundary_style(signal)
            )
        output.append(
            f" · Δ{watch.total_change_count}/W{watch.warning_count}",
            style="bold #ff4f7d" if watch.warning_count else "#9ee37d",
        )
        return output

    output = Text("BOUNDARY WATCH", style="bold #ffd08a")
    output.append(f"  ·  {watch.observation_count} OBS", style="dim")
    output.append(f"  ·  Δ{watch.total_change_count}", style="#9bcdf5")
    output.append(
        f"  ·  ALERT {watch.warning_count}",
        style="bold #ff4f7d" if watch.warning_count else "#9ee37d",
    )
    for row in (watch.signals[:2], watch.signals[2:]):
        output.append("\n")
        for index, signal in enumerate(row):
            if index:
                output.append("  ·  ", style="dim")
            output.append(_boundary_marker(signal), style=_boundary_style(signal))
            output.append(
                f" {_SECURITY_BOUNDARY_LABELS[signal.key]} "
                f"{_boundary_change_token(signal)}",
                style=_boundary_style(signal),
            )

    output.append("\nRECENT CHANGES", style="bold #9bcdf5")
    if not watch.changes:
        output.append("\n○ STABLE IN OBSERVATION WINDOW", style="dim")
    else:
        visible_changes = watch.changes[-max(change_limit, 1) :]
        for change in reversed(visible_changes):
            output.append("\n")
            output.append(
                "▲ " if change.severity == "warning" else "◆ ",
                style=("bold #ffb454" if change.severity == "warning" else "#9bcdf5"),
            )
            output.append(
                f"#{change.observation_index} "
                f"{_SECURITY_BOUNDARY_LABELS[change.after.key]}  "
                f"{_boundary_change_token(change.before)} → "
                f"{_boundary_change_token(change.after)}",
                style=("#ffb454" if change.severity == "warning" else "#9bcdf5"),
            )
        omitted_changes = len(watch.changes) - len(visible_changes)
        if omitted_changes or watch.dropped_change_count:
            output.append(
                f"\n… {omitted_changes + watch.dropped_change_count} EARLIER CHANGES",
                style="dim",
            )

    output.append(
        f"\nBOUNDED ALERTS  ·  {len(watch.alerts)}/{MAX_SECURITY_BOUNDARY_ALERTS}",
        style="bold #ff9fba",
    )
    if not watch.alerts:
        output.append("\n● NO ACTIVE BOUNDARY ALERTS", style="#9ee37d")
    else:
        visible_alerts = watch.alerts[-max(alert_limit, 1) :]
        for alert in reversed(visible_alerts):
            output.append("\n")
            output.append(
                "■ " if alert.severity == "critical" else "▲ ",
                style=_boundary_alert_style(alert),
            )
            output.append(
                _boundary_alert_label(alert),
                style=_boundary_alert_style(alert),
            )
        omitted_alerts = len(watch.alerts) - len(visible_alerts)
        if omitted_alerts or watch.dropped_alert_count:
            output.append(
                f"\n… {omitted_alerts + watch.dropped_alert_count} EARLIER ALERTS",
                style="dim",
            )
    return output


def format_security_boundaries(
    security: SecurityShield,
    boundary_watch: SecurityBoundaryWatch | None = None,
) -> Text:
    """Show boundary changes before concise application/OS layer truth."""

    watch = boundary_watch or project_security_boundary_watch((security,))
    output = Text("LAYER SPLIT  ·  APP ", style="bold #ff9fba")
    output.append(security.application.headline, style="bold #9ee37d")
    output.append("  ·  OS ", style="dim")
    output.append(
        security.os_sandbox.headline,
        style=(
            "bold #9ee37d" if security.os_sandbox.status == "ready" else "bold #ffb454"
        ),
    )
    output.append("  ·  APP ≠ OS", style="bold #ffb454")
    output.append("\n")
    output.append_text(format_security_boundary_watch(watch))
    return output


def _compact_approval_signal(flow: ApprovalFlow) -> str:
    if not flow.traces:
        return "APPROVAL NONE"
    trace = flow.traces[-1]
    decision = _APPROVAL_DECISION_LABELS[trace.decision]
    binding = _PREVIEW_BINDING_LABELS[trace.preview_binding].removeprefix("BINDING ")
    return f"APPROVAL {decision}/{binding}"


def _compact_boundary_signal(watch: SecurityBoundaryWatch) -> str:
    signals = " · ".join(_compact_boundary_token(item) for item in watch.signals)
    return f"{signals} · Δ{watch.total_change_count}/W{watch.warning_count}"


def _compact_boundary_token(signal: SecurityBoundarySignal) -> str:
    return {
        "path": {
            "enforced": "P OS",
            "application_only": "P APP",
            "absent": "P NONE",
        },
        "network": {
            "enforced": "N DENY",
            "absent": "N ABS",
        },
        "command": {
            "restricted": "C FIX",
            "absent": "C NONE",
        },
        "audit": {
            "recording": "A REC",
            "busy": "A BUSY",
            "disabled": "A OFF",
            "degraded": "A BAD",
            "unavailable": "A N/A",
        },
    }[signal.key][signal.state]


def _boundary_change_token(signal: SecurityBoundarySignal) -> str:
    state = {
        "path": {
            "enforced": "OS",
            "application_only": "APP",
            "absent": "NONE",
        },
        "network": {"enforced": "DENY", "absent": "ABS"},
        "command": {"restricted": "FIX", "absent": "NONE"},
        "audit": {
            "recording": "REC",
            "busy": "BUSY",
            "disabled": "OFF",
            "degraded": "BAD",
            "unavailable": "N/A",
        },
    }[signal.key][signal.state]
    if signal.key not in {"path", "network"}:
        return state
    qualifier = {
        "os_ready": "READY",
        "os_disabled": "OFF",
        "os_fail_closed": "FAIL",
        "application": "APP",
    }[signal.qualifier]
    return f"{state}/{qualifier}"


def _boundary_style(signal: SecurityBoundarySignal) -> str:
    if signal.state in {"degraded", "unavailable"}:
        return "bold #ff4f7d"
    if signal.state in {"busy", "disabled"} or signal.qualifier == "os_fail_closed":
        return "bold #ffb454"
    if signal.state == "application_only":
        return "bold #ffd08a"
    if signal.state in {"enforced", "recording", "absent"}:
        return "bold #9ee37d"
    return "bold #9bcdf5"


def _boundary_marker(signal: SecurityBoundarySignal) -> str:
    if signal.state in {"degraded", "unavailable"}:
        return "■"
    if signal.state in {"busy", "disabled"} or signal.qualifier == "os_fail_closed":
        return "▲"
    if signal.state == "application_only":
        return "◆"
    return "●"


def _boundary_alert_style(alert: SecurityBoundaryAlert) -> str:
    return {
        "information": "#9bcdf5",
        "warning": "bold #ffb454",
        "critical": "bold #ff4f7d",
    }[alert.severity]


def _boundary_alert_label(alert: SecurityBoundaryAlert) -> str:
    label = {
        "os_disabled": "OS LAYER OFF · APP GUARDS ACTIVE",
        "os_fail_closed": "OS FAIL-CLOSED · APP GUARDS ACTIVE",
        "audit_busy": "AUDIT CHECK BUSY · RETRY NEXT VIEW",
        "audit_disabled": "AUDIT RECORDING OFF",
        "audit_degraded": "AUDIT INVALID RECORDS",
        "audit_unavailable": "AUDIT CHECK UNAVAILABLE",
        "observation_failed": "OBSERVER FAILED · LAST SAFE SNAPSHOT",
        "boundary_downgrade": f"{alert.scope.upper()} POSTURE DOWNGRADE",
    }[alert.code]
    if alert.occurrences > 1:
        return f"{label}  ×{alert.occurrences}"
    return label


def _approval_trace_style(trace: ApprovalTrace) -> str:
    if trace.preview_binding in {"changed", "unavailable"}:
        return "bold #ff4f7d"
    return {
        "pending": "bold #ffb454",
        "approved": "bold #9ee37d",
        "rejected": "dim",
        "unavailable": "bold #ff4f7d",
        "error": "bold #ff4f7d",
    }[trace.decision]


def _approval_trace_marker(trace: ApprovalTrace) -> str:
    if trace.preview_binding in {"changed", "unavailable"}:
        return "▲"
    return {
        "pending": "◇",
        "approved": "●",
        "rejected": "○",
        "unavailable": "▲",
        "error": "▲",
    }[trace.decision]


def visible_node_ids(
    graph: ExecutionGraph,
    node_filter: NodeFilter,
) -> frozenset[str]:
    """Return matching nodes plus ancestors needed to preserve tree context."""

    nodes = {node.correlation_id: node for node in graph.nodes}
    if node_filter == "all":
        return frozenset(nodes)
    matched = {
        node.correlation_id
        for node in graph.nodes
        if (
            node.status in {"started", "waiting"}
            if node_filter == "active"
            else node.status == "failed"
            if node_filter == "failed"
            else node.stage in {"tool_call", "approval"}
        )
    }
    visible = set(matched)
    for correlation_id in tuple(matched):
        parent = nodes[correlation_id].parent_correlation_id
        while parent is not None and parent in nodes and parent not in visible:
            visible.add(parent)
            parent = nodes[parent].parent_correlation_id
    return frozenset(visible)


def format_context_layers(
    context: ContextTomography,
    *,
    compact: bool = False,
) -> Text:
    """Render five fixed, metadata-only context layers."""

    output = Text()
    total_chars = max(context.estimated_chars, 1)
    for index, layer in enumerate(context.layers):
        label = _CONTEXT_LAYER_LABELS[layer.kind]
        style = _CONTEXT_LAYER_STYLES[layer.kind]
        output.append("◆ ", style=style)
        output.append(f"{label:<20}" if not compact else f"{label:<15}", style=style)
        if not compact:
            meter_width = 10
            filled = round(layer.chars / total_chars * meter_width)
            output.append("━" * filled, style=style)
            output.append("─" * (meter_width - filled), style="dim")
            output.append(
                f"  {layer.chars:,} c · ~{layer.estimated_tokens:,} t"
                f" · {layer.item_count} {_CONTEXT_LAYER_UNITS[layer.kind]}",
                style="dim" if layer.item_count == 0 else "",
            )
        else:
            output.append(
                f" {layer.chars:>7,}c · ~{layer.estimated_tokens:>6,}t",
                style="dim" if layer.item_count == 0 else "",
            )
            if layer.kind == "current_chain" and layer.item_count:
                output.append(" · SUBMIT", style="#68b5ff")
        if index < len(context.layers) - 1:
            output.append("\n")
    return output


def format_context_insights(
    context: ContextTomography,
    simulation: ContextWhatIf | None = None,
    *,
    compact: bool = False,
) -> Text:
    """Render bounded history insights without result bodies or identifiers."""

    insights = Text()
    checkpoint_style = {
        "none": "dim",
        "kept": "#9ee37d",
        "omitted": "yellow",
    }[context.checkpoint_state]
    checkpoint_label = context.checkpoint_state.upper()
    largest = context.tool_results.largest
    if compact:
        pressure = context_budget_pressure(context)
        insights.append("LOAD ", style="dim")
        insights.append(
            f"{_CONTEXT_PRESSURE_LABELS[pressure.level]} "
            f"{_pressure_percent(pressure):.1f}%",
            style=_CONTEXT_PRESSURE_STYLES[pressure.level],
        )
        insights.append(
            f" · CUT {context.omitted_rounds} "
            f"{_compact_count(context.omitted_history_chars)}c/"
            f"~{_compact_count(context.omitted_history_tokens)}t · CP ",
            style="yellow" if context.omitted_rounds else "#9ee37d",
        )
        insights.append(f"{checkpoint_label}\n", style=checkpoint_style)
        if largest:
            footprint = largest[0]
            state_style = "#9ee37d" if footprint.state == "kept" else "yellow"
            insights.append(
                f"TOOL↑ #{footprint.ordinal:02d} {footprint.chars:,}c"
                f"/~{footprint.estimated_tokens:,}t · "
            )
            insights.append(footprint.state.upper(), style=state_style)
            insights.append(" · NO BODY\n", style="dim")
        else:
            insights.append("TOOL RESULTS · NONE\n", style="dim")
        usage = context.last_server_usage
        insights.append("SERVER HIST · ", style="dim")
        if usage is None:
            insights.append("NO MEASUREMENT", style="dim")
        else:
            insights.append(
                f"IN {_reported_input_tokens(usage):,} · OUT {usage.output_tokens:,}",
                style="#9bcdf5",
            )
        if simulation is not None:
            projected = simulation.projected_pressure
            insights.append("\nWHAT-IF ", style="dim")
            insights.append(
                f"+{_compact_count(simulation.additional_chars)}c → "
                f"{_CONTEXT_PRESSURE_LABELS[projected.level]} "
                f"{_pressure_percent(projected):.1f}%",
                style=_CONTEXT_PRESSURE_STYLES[projected.level],
            )
            insights.append(
                f" · CUT +{simulation.newly_omitted_rounds}",
                style="yellow" if simulation.newly_omitted_rounds else "dim",
            )
        return insights

    insights.append("HISTORY / COMPACTION\n", style="dim")
    insights.append(
        f"KEPT {context.selected_rounds}/{context.stored_rounds} ROUNDS"
        f" · CUT {context.omitted_rounds}"
        f" · {context.omitted_history_chars:,}c / "
        f"~{context.omitted_history_tokens:,}t\n",
        style="yellow" if context.omitted_rounds else "#9ee37d",
    )
    current = context.layer("current_chain")
    current_label = "SUBMIT SNAPSHOT" if current.item_count else "IDLE"
    insights.append("CHECKPOINT ", style="dim")
    insights.append(checkpoint_label, style=checkpoint_style)
    insights.append(f" · CURRENT {current_label}\n", style="#68b5ff")
    insights.append("LARGEST TOOL RESULTS · BODY HIDDEN\n", style="dim")
    if not largest:
        insights.append("NONE IN STORED HISTORY", style="dim")
    else:
        for index, footprint in enumerate(largest):
            state_style = "#9ee37d" if footprint.state == "kept" else "yellow"
            insights.append(f"#{footprint.ordinal:02d}  ", style="#8fa9bd")
            insights.append(
                f"{footprint.chars:,}c · ~{footprint.estimated_tokens:,}t · "
            )
            insights.append(footprint.state.upper(), style=state_style)
            if index < len(largest) - 1:
                insights.append("\n")
    return insights


def format_context_detail(
    context: ContextTomography,
    simulation: ContextWhatIf | None = None,
) -> Text:
    """Separate the next-input estimate from historical server measurement."""

    detail = Text()
    pressure = context_budget_pressure(context)
    detail.append("LOCAL NEXT INPUT · ESTIMATE\n", style="bold #91f5e9")
    detail.append("PRESSURE ", style="dim")
    detail.append(
        f"{_CONTEXT_PRESSURE_LABELS[pressure.level]} "
        f"{_pressure_percent(pressure):.1f}%",
        style=_CONTEXT_PRESSURE_STYLES[pressure.level],
    )
    detail.append(
        f" · {_pressure_dimension_label(pressure)} LIMIT\n",
        style="dim",
    )
    detail.append("CHAR  ", style="dim")
    detail.append_text(
        _context_meter(
            context.estimated_chars,
            context.budget_chars,
            style="#91f5e9",
        )
    )
    detail.append("\nTOKEN ", style="dim")
    if context.budget_tokens is None:
        detail.append(
            f"~{context.estimated_tokens:,} · NO LIMIT",
            style="#d7b7ff",
        )
    else:
        detail.append_text(
            _context_meter(
                context.estimated_tokens,
                context.budget_tokens,
                style="#d7b7ff",
            )
        )
    detail.append("\nLOCAL WHAT-IF · NO MODEL CALL\n", style="bold #ffb454")
    if simulation is None:
        detail.append("F4 TO SIMULATE +N INPUT CHARS\n", style="dim")
        detail.append("ASCII ESTIMATE · HISTORY UNCHANGED\n", style="dim")
    else:
        projected = simulation.projected_pressure
        detail.append(
            f"+{simulation.additional_chars:,} INPUT CHARS · ASCII ESTIMATE\n",
            style="#ffb454",
        )
        detail.append(
            f"PROJECTED {simulation.projected_chars:,}c"
            f" / ~{simulation.projected_tokens:,}t\n"
        )
        detail.append("PRESSURE ", style="dim")
        detail.append(
            f"{_CONTEXT_PRESSURE_LABELS[projected.level]} "
            f"{_pressure_percent(projected):.1f}%",
            style=_CONTEXT_PRESSURE_STYLES[projected.level],
        )
        detail.append(
            f" · {_pressure_dimension_label(projected)} LIMIT\n",
            style="dim",
        )
        detail.append(
            f"HISTORY {simulation.selected_rounds_before}→"
            f"{simulation.selected_rounds_after} KEPT"
            f" · CUT +{simulation.newly_omitted_rounds}\n",
            style="yellow" if simulation.newly_omitted_rounds else "dim",
        )
    detail.append("F4 EDIT · 0 CLEAR\n", style="dim")
    detail.append("LAST SERVER MEASUREMENT\n", style="bold #9bcdf5")
    usage = context.last_server_usage
    if usage is None:
        detail.append("NO SUCCESSFUL TURN USAGE\n", style="dim")
        detail.append("HISTORICAL · NOT A FORECAST", style="yellow")
    else:
        detail.append(
            f"LAST TURN · IN {_reported_input_tokens(usage):,}"
            f" · OUT {usage.output_tokens:,}\n",
            style="#9bcdf5",
        )
        detail.append(
            f"CACHE CREATE {usage.cache_creation_input_tokens:,}"
            f" · READ {usage.cache_read_input_tokens:,}\n",
            style="dim",
        )
        detail.append(f"TOTAL {usage.total_tokens:,} · ", style="#9bcdf5")
        detail.append("HISTORICAL · NOT A FORECAST", style="yellow")
    return detail


def _reported_input_tokens(usage: TokenUsage) -> int:
    return (
        usage.input_tokens
        + usage.cache_creation_input_tokens
        + usage.cache_read_input_tokens
    )


def _compact_context_signal(
    context: ContextTomography,
    simulation: ContextWhatIf | None = None,
) -> str:
    pressure = context_budget_pressure(context)
    pressure_signal = (
        f"{_CONTEXT_PRESSURE_LABELS[pressure.level]}{_pressure_percent(pressure):.0f}%"
    )
    checkpoint = {
        "none": "CP—",
        "kept": "CP✓",
        "omitted": "CP×",
    }[context.checkpoint_state]
    if simulation is not None:
        projected = simulation.projected_pressure
        return (
            f"{pressure_signal} · CUT{context.omitted_rounds} · {checkpoint} · "
            f"IF+{_compact_count(simulation.additional_chars)}→"
            f"{_CONTEXT_PRESSURE_LABELS[projected.level]}"
            f"{_pressure_percent(projected):.0f}% · "
            f"+{simulation.newly_omitted_rounds}CUT"
        )
    largest = context.tool_results.largest
    tool_signal = f"TOOL↑{_compact_count(largest[0].chars)}c" if largest else "TOOL—"
    usage = context.last_server_usage
    server_signal = (
        f"SRV{_compact_count(_reported_input_tokens(usage))}t HIST"
        if usage is not None
        else "SRV—"
    )
    return (
        f"{pressure_signal} · CUT{context.omitted_rounds} · {checkpoint} · "
        f"{tool_signal} · {server_signal}"
    )


def _pressure_percent(pressure: ContextBudgetPressure) -> float:
    return pressure.limiting_basis_points / 100


def _pressure_dimension_label(pressure: ContextBudgetPressure) -> str:
    return "TOKEN" if pressure.limiting_dimension == "tokens" else "CHAR"


def _compact_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k"
    return f"{value / 1_000_000:.1f}m"


def _context_meter(value: int, total: int, *, style: str) -> Text:
    ratio = value / max(total, 1)
    filled = round(min(max(ratio, 0.0), 1.0) * 12)
    meter = Text()
    meter.append("━" * filled, style=style)
    meter.append("─" * (12 - filled), style="dim")
    meter.append(f" {value:,}/{total:,} · {ratio * 100:4.1f}%", style="dim")
    return meter


def format_node_label(
    node: ExecutionNode,
    *,
    approval_trace: ApprovalTrace | None = None,
) -> Text:
    """Create one compact label without interpreting metadata as markup."""

    style = (
        _approval_trace_style(approval_trace)
        if approval_trace is not None
        else _STATUS_STYLE[node.status]
    )
    marker = (
        _approval_trace_marker(approval_trace)
        if approval_trace is not None
        else _STATUS_MARKER[node.status]
    )
    label = Text(f"{marker} ", style=style)
    label.append(node.stage.replace("_", " ").upper(), style=style)
    tool_name = _metadata_value(node, "tool_name")
    if tool_name is not None:
        label.append(f"  {_safe_inline(tool_name)}", style="bold")
    if approval_trace is not None:
        decision = _APPROVAL_DECISION_LABELS[approval_trace.decision]
        binding = _PREVIEW_BINDING_LABELS[approval_trace.preview_binding].removeprefix(
            "BINDING "
        )
        label.append(f"  {decision}/{binding}", style=style)
    label.append(f"  {_short_id(node.correlation_id)}", style="dim")
    if node.anomalies:
        label.append(f"  ⚠{len(node.anomalies)}", style="yellow")
    return label


def format_node_detail(
    node: ExecutionNode,
    *,
    approval_trace: ApprovalTrace | None = None,
) -> Text:
    """Render only IDs, times, status, anomalies, and allowlisted metadata."""

    detail = Text()
    detail.append("STATUS\n", style="dim")
    detail.append(
        f"{_STATUS_MARKER[node.status]} {node.status.upper()}\n\n",
        style=_STATUS_STYLE[node.status],
    )
    detail.append("STAGE\n", style="dim")
    detail.append(f"{node.stage}\n\n", style="bold")
    if approval_trace is not None:
        detail.append("APPROVAL TRACE\n", style="bold #ffb454")
        detail.append("DECISION  ", style="dim")
        detail.append(
            f"{_APPROVAL_DECISION_LABELS[approval_trace.decision]}\n",
            style=_approval_trace_style(approval_trace),
        )
        detail.append("PREVIEW BINDING  ", style="dim")
        detail.append(
            f"{_PREVIEW_BINDING_LABELS[approval_trace.preview_binding]}\n",
            style=_approval_trace_style(approval_trace),
        )
        detail.append("TOOL NODE  ", style="dim")
        detail.append(f"{approval_trace.tool_correlation_id or 'UNRESOLVED'}\n")
        detail.append("PREVIEW SIZE  ", style="dim")
        detail.append(f"{approval_trace.preview_chars:,} CHARS · BODY HIDDEN\n\n")
    detail.append("CORRELATION\n", style="dim")
    detail.append(f"{node.correlation_id}\n\n")
    detail.append("PARENT\n", style="dim")
    detail.append(f"{node.parent_correlation_id or 'ROOT'}\n\n")
    detail.append("STARTED\n", style="dim")
    detail.append(f"{_format_time(node.started_at)}\n\n")
    detail.append("FINISHED\n", style="dim")
    detail.append(f"{_format_time(node.finished_at)}\n")
    metadata = (*node.start_metadata, *node.finish_metadata)
    if metadata:
        detail.append("\nSAFE METADATA\n", style="dim")
        for item in metadata:
            if approval_trace is not None and item.name in {
                "approval_decision",
                "preview_binding",
            }:
                continue
            detail.append(f"{item.name}  ", style="#8fa9bd")
            detail.append(f"{_safe_inline(item.value)}\n")
    if node.anomalies:
        detail.append("\nANOMALIES\n", style="yellow")
        for anomaly in node.anomalies:
            detail.append(f"• {anomaly}\n", style="yellow")
    return detail


def format_metrics(
    metrics: RuntimeMetrics,
    *,
    filter_name: str,
    bus_dropped: int,
    view_dropped: int,
    busy: bool,
    cancelling: bool,
    compact: bool = False,
) -> Text:
    """Render a compact, responsive telemetry strip."""

    state = "CANCELLING" if cancelling else "RUNNING" if busy else "READY"
    state_style = "yellow" if cancelling else "bright_cyan" if busy else "green"
    line = Text()
    line.append(state, style=f"bold {state_style}")
    line.append(f"  ·  FILTER {filter_name}", style="dim")
    line.append(
        f"  ·  NODES {metrics.total_nodes} / ACTIVE {metrics.active_nodes}",
        style="#c6d4df",
    )
    line.append(f"  ·  FAIL {metrics.failed_nodes}", style="red")
    if compact:
        if bus_dropped or view_dropped:
            line.append(
                f"  ·  DROPPED {bus_dropped + view_dropped}",
                style="bold yellow",
            )
        return line
    line.append(
        f"  ·  MODEL {metrics.model_requests} / TOOLS {metrics.tool_calls}",
        style="#c6d4df",
    )
    line.append(
        f"  ·  TOKENS {metrics.input_tokens:,}↓ {metrics.output_tokens:,}↑",
        style="#91f5e9",
    )
    line.append(
        f"  ·  Σ {metrics.reported_elapsed_ms:,}ms",
        style="#d7b7ff",
    )
    if bus_dropped or view_dropped:
        line.append(
            f"  ·  DROPPED {bus_dropped + view_dropped}",
            style="bold yellow",
        )
    return line


def _metadata_value(
    node: ExecutionNode,
    name: str,
) -> bool | int | str | None:
    return next(
        (
            item.value
            for item in (*node.start_metadata, *node.finish_metadata)
            if item.name == name
        ),
        None,
    )


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _time_label(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%H:%M:%S")


def _short_id(value: str) -> str:
    prefix, _, token = value.partition("-")
    return f"{prefix}-{token[:8]}"


def _safe_inline(value: object, *, max_chars: int = 240) -> str:
    normalized = " ".join(str(value).split())
    cleaned = "".join(
        character for character in normalized if not category(character).startswith("C")
    )
    return cleaned[:max_chars] or "N/A"


def _safe_multiline(value: str, max_chars: int | None = None) -> str:
    cleaned = "".join(
        character
        for character in value
        if character in "\n\t" or not category(character).startswith("C")
    )
    if max_chars is not None:
        return cleaned[:max_chars]
    return cleaned
