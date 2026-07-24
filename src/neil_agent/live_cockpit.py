"""Full-screen Textual cockpit driven by runtime-event projections."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event, Lock
from typing import Literal, Protocol
from unicodedata import category

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Log, Static, Tree
from textual.widgets.tree import TreeNode

from .errors import NeilAgentError
from .events import EventBus, EventSubscription, RuntimeEvent
from .projections import (
    ExecutionGraph,
    ExecutionGraphProjector,
    ExecutionNode,
    MetricsProjector,
    RuntimeMetrics,
)
from .schemas import ToolCall

MAX_LIVE_EVENTS = 10_000
MAX_BRIDGE_EVENTS = 1_024
MAX_LIVE_OUTPUT_LINES = 500
MAX_LIVE_ERROR_CHARS = 500
MAX_APPROVAL_PREVIEW_CHARS = 20_000
NARROW_TERMINAL_WIDTH = 88

NodeFilter = Literal["all", "active", "failed", "tools"]

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


class LiveAgent(Protocol):
    """Agent surface needed by the live cockpit."""

    def stream_chat(self, user_input: str) -> Iterator[str]: ...


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


class LiveCockpitApp(App[None]):
    """Interactive Agent shell with a metadata-only live execution tree."""

    TITLE = "Neil // Live Mission Control"
    SUB_TITLE = "metadata-only runtime projection"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("ctrl+q", "request_exit", "退出"),
        Binding("ctrl+x", "cancel_turn", "取消请求"),
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
        padding: 0 1;
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

    #conversation {
        height: 12;
        margin: 0 1;
        border-top: tall #223444;
        background: #080d14;
    }

    #stream-title {
        height: 2;
        padding: 0 1;
        color: #8fa9bd;
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

    LiveCockpitApp.narrow #workspace {
        layout: horizontal;
    }

    LiveCockpitApp.narrow #dag-panel {
        width: 1fr;
        min-width: 0;
        margin-right: 0;
    }

    LiveCockpitApp.narrow #detail-panel {
        display: none;
    }

    LiveCockpitApp.narrow #conversation {
        height: 7;
    }

    LiveCockpitApp.narrow #stream-title {
        display: none;
    }

    LiveCockpitApp.narrow #brand {
        height: 3;
        padding: 0 1;
        content-align: left middle;
    }
    """

    def __init__(
        self,
        agent: LiveAgent,
        event_bus: EventBus,
        *,
        model: str,
        workspace: str,
        initial_events: Iterable[RuntimeEvent] = (),
        max_events: int = MAX_LIVE_EVENTS,
    ) -> None:
        if max_events < 1:
            raise ValueError("live cockpit event capacity must be at least 1")
        super().__init__()
        self._agent = agent
        self._event_bus = event_bus
        self._model = _safe_inline(model)
        self._workspace = _safe_inline(workspace)
        materialized_events = tuple(initial_events)
        self._events = list(materialized_events[-max_events:])
        self._max_events = max_events
        self._view_dropped_events = max(
            len(materialized_events) - max_events,
            0,
        )
        self._graph = ExecutionGraphProjector().project(self._events)
        self._metrics = MetricsProjector().project(self._graph)
        self._filter: NodeFilter = "all"
        self._selected_correlation_id: str | None = None
        self._subscription: EventSubscription | None = None
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

    def compose(self) -> ComposeResult:
        yield Static(self._brand_text(), id="brand")
        yield Static(id="metrics")
        with Horizontal(id="workspace"):
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
        with Vertical(id="conversation"):
            yield Static(
                "AGENT STREAM  ·  输出仅保留在当前有界视图",
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
        self.set_class(self.size.width < NARROW_TERMINAL_WIDTH, "narrow")
        self._subscription = self._event_bus.subscribe(self._bridge.observe)
        self._refresh_projection()
        self.set_interval(0.25, self._refresh_metrics)
        self.query_one("#prompt", Input).focus()

    def on_unmount(self) -> None:
        self._closed = True
        self._cancel_requested.set()
        subscription = self._subscription
        self._subscription = None
        if subscription is not None:
            subscription.close()
        self._bridge.close()
        self._reject_pending_approvals()

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < NARROW_TERMINAL_WIDTH, "narrow")
        self._refresh_metrics()

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
        self._cancel_requested.clear()
        self._turn_done.clear()
        transcript = self.query_one("#transcript", Log)
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

    def on_assistant_chunk(self, message: AssistantChunk) -> None:
        self.query_one("#transcript", Log).write(_safe_multiline(message.chunk))

    def on_turn_finished(self, message: TurnFinished) -> None:
        self._busy = False
        if message.succeeded:
            self.completed_turns += 1
            summary = Text("✓ 请求完成", style="green")
        elif message.cancelled:
            summary = Text("■ 请求已取消", style="yellow")
        else:
            summary = Text("▲ 请求失败", style="bold red")
        transcript = self.query_one("#transcript", Log)
        transcript.write_line(f"\n{summary.plain}")
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.focus()
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

    def action_cancel_turn(self) -> None:
        if not self._busy:
            self.notify("当前没有正在执行的请求")
            return
        self._cancel_requested.set()
        self._reject_pending_approvals()
        self.notify("已请求取消；正在等待当前操作返回", severity="warning")
        self._refresh_metrics()

    def action_filter_all(self) -> None:
        self._set_filter("all")

    def action_filter_active(self) -> None:
        self._set_filter("active")

    def action_filter_failed(self) -> None:
        self._set_filter("failed")

    def action_filter_tools(self) -> None:
        self._set_filter("tools")

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

    def _refresh_projection(self) -> None:
        self._graph = ExecutionGraphProjector().project(self._events)
        self._metrics = MetricsProjector().project(self._graph)
        self._refresh_tree()
        self._refresh_metrics()

    def _refresh_tree(self) -> None:
        tree = self.query_one("#execution-tree", Tree)
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
                format_node_label(node),
                data=node.correlation_id,
                expand=True,
            )
            added[node.correlation_id] = tree_node
            stack.extend(
                (tree_node, child)
                for child in reversed(children.get(node.correlation_id, ()))
            )

        title = self.query_one("#tree-title", Static)
        title.update(
            Text(
                f"LIVE EXECUTION TREE  ·  FILTER {_FILTER_LABELS[self._filter]}",
                style="bold #91f5e9",
            )
        )
        if not nodes:
            tree.root.add_leaf(Text("没有匹配的执行节点", style="dim"))
            self._selected_correlation_id = None
            self.query_one("#node-detail", Static).update(
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
        detail = self.query_one("#node-detail", Static)
        if self._selected_correlation_id is None:
            detail.update(Text("选择节点以查看安全元数据", style="dim"))
            return
        node = self._graph.node(self._selected_correlation_id)
        if node is None:
            detail.update(Text("节点已不在当前有界事件窗口中", style="dim"))
            return
        detail.update(format_node_detail(node))

    def _refresh_metrics(self) -> None:
        metrics = self.query_one("#metrics", Static)
        metrics.update(
            format_metrics(
                self._metrics,
                filter_name=_FILTER_LABELS[self._filter],
                bus_dropped=self._event_bus.stats.dropped_deliveries,
                view_dropped=(self._view_dropped_events + self._bridge.dropped_events),
                busy=self._busy,
                cancelling=self._cancel_requested.is_set() and self._busy,
                compact=self.has_class("narrow"),
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
    approval_handler_owner: object | None = None,
) -> int:
    """Run the app and return the number of successful turns it completed."""

    app = LiveCockpitApp(
        agent,
        event_bus,
        model=model,
        workspace=workspace,
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
            else node.stage == "tool_call"
        )
    }
    visible = set(matched)
    for correlation_id in tuple(matched):
        parent = nodes[correlation_id].parent_correlation_id
        while parent is not None and parent in nodes and parent not in visible:
            visible.add(parent)
            parent = nodes[parent].parent_correlation_id
    return frozenset(visible)


def format_node_label(node: ExecutionNode) -> Text:
    """Create one compact label without interpreting metadata as markup."""

    style = _STATUS_STYLE[node.status]
    label = Text(f"{_STATUS_MARKER[node.status]} ", style=style)
    label.append(node.stage.replace("_", " ").upper(), style=style)
    tool_name = _metadata_value(node, "tool_name")
    if tool_name is not None:
        label.append(f"  {_safe_inline(tool_name)}", style="bold")
    label.append(f"  {_short_id(node.correlation_id)}", style="dim")
    if node.anomalies:
        label.append(f"  ⚠{len(node.anomalies)}", style="yellow")
    return label


def format_node_detail(node: ExecutionNode) -> Text:
    """Render only IDs, times, status, anomalies, and allowlisted metadata."""

    detail = Text()
    detail.append("STATUS\n", style="dim")
    detail.append(
        f"{_STATUS_MARKER[node.status]} {node.status.upper()}\n\n",
        style=_STATUS_STYLE[node.status],
    )
    detail.append("STAGE\n", style="dim")
    detail.append(f"{node.stage}\n\n", style="bold")
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
