"""Tests for the Textual live execution-tree cockpit."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from threading import Thread

import pytest
from textual.widgets import ContentSwitcher, Input, Log, Tree

from neil_agent.context import (
    ContextLayerEstimate,
    ContextTomography,
    ContextToolResultFootprint,
    ContextToolResultInsights,
)
from neil_agent.events import (
    EventBus,
    RuntimeEvent,
    RuntimeEventEmitter,
    RuntimeMetadataItem,
    RuntimeStage,
    RuntimeStatus,
)
from neil_agent.live_cockpit import (
    LiveCockpitApp,
    RuntimeEventBridge,
    ToolApprovalScreen,
    format_context_detail,
    format_context_insights,
    format_context_layers,
    format_node_detail,
    run_live_cockpit,
    visible_node_ids,
)
from neil_agent.projections import ExecutionGraphProjector
from neil_agent.schemas import TokenUsage, ToolCall

NOW = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)


def _context_snapshot(current_input: str = "") -> ContextTomography:
    current_chars = len(current_input)
    return ContextTomography(
        budget_chars=120_000,
        budget_tokens=64_000,
        stored_rounds=3,
        selected_rounds=2,
        omitted_rounds=1,
        stored_history_chars=12_000,
        stored_history_tokens=3_600,
        layers=(
            ContextLayerEstimate("system", 6_000, 1_800, 1),
            ContextLayerEstimate("tool_schemas", 4_000, 1_200, 12),
            ContextLayerEstimate("project_instructions", 1_000, 300, 1),
            ContextLayerEstimate("selected_history", 8_000, 2_400, 6),
            ContextLayerEstimate(
                "current_chain",
                current_chars,
                current_chars // 3,
                int(bool(current_input)),
            ),
        ),
        last_server_usage=TokenUsage(
            input_tokens=1_200,
            output_tokens=120,
            cache_creation_input_tokens=300,
            cache_read_input_tokens=700,
        ),
        checkpoint_state="kept",
        tool_results=ContextToolResultInsights(
            stored_count=2,
            selected_count=1,
            largest=(
                ContextToolResultFootprint(1, 3_200, 960, "omitted"),
                ContextToolResultFootprint(2, 1_800, 540, "kept"),
            ),
        ),
    )


def _event(
    number: int,
    *,
    correlation_id: str,
    stage: RuntimeStage,
    status: RuntimeStatus,
    offset_ms: int,
    parent_event_id: str | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=f"evt-{number:032x}",
        correlation_id=correlation_id,
        parent_event_id=parent_event_id,
        timestamp=NOW + timedelta(milliseconds=offset_ms),
        stage=stage,
        status=status,
    )


def _execution_events() -> tuple[RuntimeEvent, ...]:
    turn_id = "turn-" + "a" * 32
    model_id = "model-" + "b" * 32
    tool_id = "tool-" + "c" * 32
    turn = _event(
        1,
        correlation_id=turn_id,
        stage="agent_turn",
        status="started",
        offset_ms=0,
    )
    model = _event(
        2,
        correlation_id=model_id,
        stage="model_request",
        status="started",
        offset_ms=1,
        parent_event_id=turn.event_id,
    )
    tool = _event(
        3,
        correlation_id=tool_id,
        stage="tool_call",
        status="started",
        offset_ms=2,
        parent_event_id=model.event_id,
    ).model_copy(
        update={
            "metadata": (
                RuntimeMetadataItem(
                    name="tool_name",
                    value="[bold red]read_file[/bold red]",
                ),
            )
        }
    )
    failed = _event(
        4,
        correlation_id=tool_id,
        stage="tool_call",
        status="failed",
        offset_ms=3,
        parent_event_id=tool.event_id,
    )
    return turn, model, tool, failed


class FakeLiveAgent:
    def __init__(self, bus: EventBus) -> None:
        self._emitter = RuntimeEventEmitter(bus)
        self.prompts: list[str] = []
        self.tomography_inputs: list[str] = []

    def context_tomography(self, current_input: str = "") -> ContextTomography:
        self.tomography_inputs.append(current_input)
        return _context_snapshot(current_input)

    def stream_chat(self, user_input: str) -> Iterator[str]:
        self.prompts.append(user_input)
        span = self._emitter.start(
            "agent_turn",
            metadata={
                "input_chars": len(user_input),
                "history_messages": 0,
                "history_rounds": 0,
                "selected_messages": 0,
                "omitted_rounds": 0,
            },
        )
        yield "hello "
        self._emitter.finish(
            span,
            "succeeded",
            metadata={
                "model_requests": 1,
                "tool_calls": 0,
                "response_chars": 11,
                "elapsed_ms": 8,
            },
        )
        yield "world"


class FakeApprovalOwner:
    def __init__(self) -> None:
        self.handler: object = "classic-handler"

    def replace_approval_handler(self, handler: object) -> object:
        previous = self.handler
        self.handler = handler
        return previous


def test_event_bridge_coalesces_notifications_and_bounds_memory() -> None:
    notifications: list[str] = []
    bridge = RuntimeEventBridge(
        lambda: notifications.append("ready"),
        max_events=2,
    )
    events = _execution_events()

    for event in events[:3]:
        bridge.observe(event)

    assert notifications == ["ready"]
    assert bridge.dropped_events == 1
    assert bridge.drain() == events[1:3]

    bridge.observe(events[3])
    assert notifications == ["ready", "ready"]
    bridge.close()
    bridge.observe(events[0])
    assert bridge.drain() == ()


def test_tree_filters_keep_ancestors_and_details_treat_markup_as_text() -> None:
    graph = ExecutionGraphProjector().project(_execution_events())
    turn_id, model_id, tool_id = (
        "turn-" + "a" * 32,
        "model-" + "b" * 32,
        "tool-" + "c" * 32,
    )

    assert visible_node_ids(graph, "tools") == {turn_id, model_id, tool_id}
    assert visible_node_ids(graph, "failed") == {turn_id, model_id, tool_id}
    assert visible_node_ids(graph, "active") == {turn_id, model_id}

    tool_node = graph.node(tool_id)
    assert tool_node is not None
    detail = format_node_detail(tool_node)
    assert "[bold red]read_file[/bold red]" in detail.plain
    assert detail.spans


def test_context_tomography_formatters_are_metadata_only() -> None:
    context = _context_snapshot("current")

    layers = format_context_layers(context)
    compact_layers = format_context_layers(context, compact=True)
    insights = format_context_insights(context)
    compact_insights = format_context_insights(context, compact=True)
    detail = format_context_detail(context)

    assert "SYSTEM FIXED" in layers.plain
    assert "CURRENT CHAIN" in compact_layers.plain
    assert "LARGEST TOOL RESULTS · BODY HIDDEN" in insights.plain
    assert "#01" in insights.plain
    assert "SERVER HIST" in compact_insights.plain
    assert "CHARACTER SOFT BUDGET" in detail.plain
    assert "LAST SERVER MEASUREMENT" in detail.plain
    assert "HISTORICAL · NOT A FORECAST" in detail.plain


@pytest.mark.asyncio
async def test_live_app_streams_agent_events_and_output() -> None:
    bus = EventBus(queue_size=32)
    agent = FakeLiveAgent(bus)
    app = LiveCockpitApp(
        agent,
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
    )

    async with app.run_test(size=(120, 40)) as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.value = "inspect project"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert agent.prompts == ["inspect project"]
        assert agent.tomography_inputs == ["", "inspect project", ""]
        assert app.completed_turns == 1
        assert app.metrics.total_nodes == 1
        assert app.metrics.succeeded_nodes == 1
        assert app.graph.nodes[0].stage == "agent_turn"
        transcript = app.query_one("#transcript", Log)
        assert "hello world" in "\n".join(transcript.lines)

    assert bus.close()


@pytest.mark.asyncio
async def test_live_app_switches_between_execution_and_context_views() -> None:
    bus = EventBus()
    app = LiveCockpitApp(
        FakeLiveAgent(bus),
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        switcher = app.query_one("#workspace", ContentSwitcher)
        workspace = switcher.region
        conversation = app.query_one("#conversation").region

        assert app.monitor_view == "execution"
        assert switcher.current == "execution-view"

        await pilot.press("f3")
        await pilot.pause()

        assert app.monitor_view == "context"
        assert switcher.current == "context-view"
        assert switcher.region == workspace
        assert app.query_one("#conversation").region == conversation
        assert "SYSTEM FIXED" in app.query_one("#context-layers").render().plain
        assert "LOCAL ESTIMATE" in app.query_one("#context-title").render().plain
        assert "BODY HIDDEN" in app.query_one("#context-insights").render().plain
        assert (
            "LAST SERVER MEASUREMENT" in app.query_one("#context-detail").render().plain
        )
        assert app.check_action("filter_tools", ()) is False
        assert app.query_one("#prompt", Input).has_focus

        await pilot.press("f3")
        await pilot.pause()

        assert app.monitor_view == "execution"
        assert switcher.current == "execution-view"
        assert app.query_one("#prompt", Input).has_focus

    assert bus.close()


@pytest.mark.asyncio
async def test_live_app_filters_and_adapts_to_narrow_terminal() -> None:
    bus = EventBus()
    app = LiveCockpitApp(
        FakeLiveAgent(bus),
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
        initial_events=_execution_events(),
    )

    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        assert app.has_class("narrow")
        assert app.has_class("short")
        assert not app.query_one("#detail-panel").display
        assert app.query_one("#transcript", Log).region.height >= 5
        assert app.query_one("#prompt", Input).display
        app.action_filter_tools()
        await pilot.pause()
        tree = app.query_one("#execution-tree", Tree)
        assert app.node_filter == "tools"
        assert len(tree.root.children) == 1
        assert "FILTER TOOLS" in app.query_one("#tree-title").render().plain

        await pilot.press("f3")
        await pilot.pause()

        assert app.monitor_view == "context"
        assert app.query_one("#context-panel").display
        assert not app.query_one("#context-detail-panel").display
        assert not app.query_one("#context-insights").display
        assert "CURRENT CHAIN" in app.query_one("#context-layers").render().plain
        assert app.query_one("#context-layers").region.height >= 5
        assert "SRV" in str(app.query_one("#context-panel").border_subtitle)
        assert app.query_one("#transcript", Log).region.height >= 5

        await pilot.resize_terminal(60, 40)
        await pilot.pause()

        assert app.has_class("narrow")
        assert not app.has_class("short")
        assert app.query_one("#context-title").display
        assert app.query_one("#context-panel").border_title is None
        assert app.query_one("#context-insights").display
        compact_insights = app.query_one("#context-insights").render().plain
        assert "TOOL↑" in compact_insights
        assert "SERVER HIST" in compact_insights
        assert app.query_one("#context-insights").region.height >= 3

    assert bus.close()


@pytest.mark.asyncio
async def test_live_app_balances_output_and_adapts_to_terminal_height() -> None:
    bus = EventBus()
    app = LiveCockpitApp(
        FakeLiveAgent(bus),
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app.query_one("#workspace")
        conversation = app.query_one("#conversation")
        transcript = app.query_one("#transcript", Log)

        assert not app.has_class("narrow")
        assert not app.has_class("short")
        assert abs(workspace.region.height - conversation.region.height) <= 1
        assert conversation.region.height >= 14
        assert transcript.region.height >= 7

        await pilot.resize_terminal(120, 24)
        await pilot.pause()

        assert not app.has_class("narrow")
        assert app.has_class("short")
        assert conversation.region.height > workspace.region.height
        assert transcript.region.height >= 5
        assert app.query_one("#metrics").region.height == 1

        await pilot.resize_terminal(120, 36)
        await pilot.pause()

        assert not app.has_class("short")
        assert abs(workspace.region.height - conversation.region.height) <= 1
        assert conversation.region.height >= 14

    assert bus.close()


@pytest.mark.asyncio
async def test_live_app_expands_output_and_restores_the_selected_monitor() -> None:
    bus = EventBus()
    app = LiveCockpitApp(
        FakeLiveAgent(bus),
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        workspace = app.query_one("#workspace")
        transcript = app.query_one("#transcript", Log)
        prompt = app.query_one("#prompt", Input)
        transcript.write_line("preserved output")
        baseline_height = transcript.region.height

        await pilot.press("f2")
        await pilot.pause()

        assert app.output_expanded
        assert not workspace.display
        assert transcript.region.height > baseline_height
        assert prompt.has_focus
        assert "F2 返回执行树" in app.query_one("#stream-title").render().plain
        assert "preserved output" in transcript.lines

        await pilot.press("f3")
        await pilot.pause()

        assert app.monitor_view == "context"
        assert app.query_one("#workspace", ContentSwitcher).current == "context-view"
        assert "F2 返回上下文" in app.query_one("#stream-title").render().plain

        await pilot.press("ctrl+o")
        await pilot.pause()

        assert not app.output_expanded
        assert workspace.display
        assert transcript.region.height == baseline_height
        assert prompt.has_focus
        assert "F2 展开结果" in app.query_one("#stream-title").render().plain
        assert app.query_one("#workspace", ContentSwitcher).current == "context-view"
        assert "preserved output" in transcript.lines

    assert bus.close()


@pytest.mark.asyncio
async def test_live_approval_modal_returns_explicit_decision() -> None:
    bus = EventBus()
    app = LiveCockpitApp(
        FakeLiveAgent(bus),
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
    )
    decisions: list[bool] = []

    async with app.run_test(size=(100, 32)) as pilot:
        thread = Thread(
            target=lambda: decisions.append(
                app.request_tool_approval(
                    ToolCall(id="call-1", name="write_file", arguments={}),
                    "Write preview",
                )
            )
        )
        thread.start()
        await pilot.pause(0.3)
        assert isinstance(app.screen, ToolApprovalScreen)
        await pilot.press("f2")
        assert not app.output_expanded
        await pilot.press("f3")
        assert app.monitor_view == "execution"
        assert isinstance(app.screen, ToolApprovalScreen)
        await pilot.press("y")
        await pilot.pause()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert decisions == [True]

    assert bus.close()


def test_live_runner_restores_the_classic_approval_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = EventBus()
    owner = FakeApprovalOwner()
    active_handlers: list[object] = []

    def fake_run(app: LiveCockpitApp, *, mouse: bool) -> None:
        assert mouse is True
        active_handlers.append(owner.handler)
        app.completed_turns = 2

    monkeypatch.setattr(LiveCockpitApp, "run", fake_run)

    completed = run_live_cockpit(
        FakeLiveAgent(bus),
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
        approval_handler_owner=owner,
    )

    assert completed == 2
    assert len(active_handlers) == 1
    assert callable(active_handlers[0])
    assert owner.handler == "classic-handler"
    assert bus.close()
