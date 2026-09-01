"""Tests for the Textual live execution-tree cockpit."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from threading import Thread
from typing import Literal

import pytest
from textual.widgets import ContentSwitcher, Input, Log, Tree

from neil_agent.checkpoint import FileEditCheckpoint, FileTaskCheckpoint
from neil_agent.context import (
    ContextLayerEstimate,
    ContextTomography,
    ContextToolResultFootprint,
    ContextToolResultInsights,
    ContextWhatIf,
    build_context_what_if,
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
    CheckpointRestoreScreen,
    ContextWhatIfScreen,
    LiveCockpitApp,
    RuntimeEventBridge,
    ToolApprovalScreen,
    format_approval_flows,
    format_context_detail,
    format_context_insights,
    format_context_layers,
    format_neural_map_title,
    format_node_detail,
    format_node_label,
    format_security_boundaries,
    format_security_boundary_watch,
    format_security_capabilities,
    format_security_title,
    run_live_cockpit,
    visible_node_ids,
)
from neil_agent.neural_map import build_neural_map_fixture_events
from neil_agent.projections import ExecutionGraphProjector
from neil_agent.schemas import TokenUsage, ToolCall
from neil_agent.session import SessionSummary
from neil_agent.security import (
    ApprovalFlowProjector,
    SecurityShield,
    project_security_boundary_watch,
    project_security_shield,
)
from neil_agent.time_machine import TimeMachineHistory
from neil_agent.tools.filesystem import FileSystemTools

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


def _approval_execution_events(
    *,
    binding: Literal["valid", "changed"] = "valid",
) -> tuple[RuntimeEvent, ...]:
    turn_id = "turn-" + "d" * 32
    tool_id = "tool-" + "e" * 32
    approval_id = "approval-" + "f" * 32
    turn = _event(
        10,
        correlation_id=turn_id,
        stage="agent_turn",
        status="started",
        offset_ms=0,
    )
    tool = _event(
        11,
        correlation_id=tool_id,
        stage="tool_call",
        status="started",
        offset_ms=1,
        parent_event_id=turn.event_id,
    ).model_copy(
        update={
            "metadata": (
                RuntimeMetadataItem(name="tool_name", value="write_file"),
                RuntimeMetadataItem(name="argument_count", value=2),
                RuntimeMetadataItem(name="requires_approval", value=True),
            )
        }
    )
    approval = _event(
        12,
        correlation_id=approval_id,
        stage="approval",
        status="waiting",
        offset_ms=2,
        parent_event_id=tool.event_id,
    ).model_copy(
        update={
            "metadata": (
                RuntimeMetadataItem(name="tool_name", value="write_file"),
                RuntimeMetadataItem(name="preview_chars", value=320),
                RuntimeMetadataItem(name="approval_decision", value="pending"),
                RuntimeMetadataItem(name="preview_binding", value="pending"),
            )
        }
    )
    decision = _event(
        13,
        correlation_id=approval_id,
        stage="approval",
        status="succeeded",
        offset_ms=3,
        parent_event_id=approval.event_id,
    ).model_copy(
        update={
            "metadata": (
                RuntimeMetadataItem(name="approval_decision", value="approved"),
                RuntimeMetadataItem(name="preview_binding", value="pending"),
                RuntimeMetadataItem(name="elapsed_ms", value=12),
            )
        }
    )
    tool_finish = _event(
        14,
        correlation_id=tool_id,
        stage="tool_call",
        status="succeeded" if binding == "valid" else "failed",
        offset_ms=4,
        parent_event_id=tool.event_id,
    ).model_copy(
        update={
            "metadata": (
                RuntimeMetadataItem(name="approval_decision", value="approved"),
                RuntimeMetadataItem(name="preview_binding", value=binding),
                RuntimeMetadataItem(name="is_error", value=binding != "valid"),
                RuntimeMetadataItem(name="result_chars", value=24),
                RuntimeMetadataItem(name="elapsed_ms", value=20),
            )
        }
    )
    return turn, tool, approval, decision, tool_finish


def _security_snapshot() -> SecurityShield:
    return project_security_shield(
        {
            "list_directory": False,
            "read_file": False,
            "search_text": False,
            "set_task_plan": False,
            "update_task_step": False,
            "git_status": False,
            "git_diff": False,
            "write_file": True,
            "replace_text": True,
            "run_quality_check": True,
            "git_stage": True,
            "git_commit": True,
            "run_readonly_subtask": False,
            "load_skill": False,
        },
        sandbox_backend="disabled",
        audit_enabled=True,
    )


def _changed_security_snapshot() -> SecurityShield:
    return project_security_shield(
        {
            "list_directory": False,
            "read_file": False,
            "search_text": False,
            "set_task_plan": False,
            "update_task_step": False,
            "git_status": False,
            "git_diff": False,
            "write_file": True,
            "replace_text": True,
            "run_quality_check": True,
            "git_stage": True,
            "git_commit": True,
            "run_readonly_subtask": False,
            "load_skill": False,
        },
        sandbox_backend="windows-sandbox",
        audit_enabled=False,
        sandbox_probe_failed=True,
    )


class FakeLiveAgent:
    def __init__(self, bus: EventBus) -> None:
        self._emitter = RuntimeEventEmitter(bus)
        self.prompts: list[str] = []
        self.tomography_inputs: list[str] = []
        self.what_if_inputs: list[int] = []

    def context_tomography(self, current_input: str = "") -> ContextTomography:
        self.tomography_inputs.append(current_input)
        return _context_snapshot(current_input)

    def context_what_if(self, additional_chars: int) -> ContextWhatIf:
        self.what_if_inputs.append(additional_chars)
        return build_context_what_if(
            _context_snapshot(),
            _context_snapshot("x" * additional_chars),
            additional_chars=additional_chars,
        )

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


def _time_machine_history() -> TimeMachineHistory:
    root_id = "20260823T120000000000Z-aaaaaaaa"
    branch_id = "20260823T120001000000Z-bbbbbbbb"
    return TimeMachineHistory(
        sessions=(
            SessionSummary(
                session_id=root_id,
                title="PRIVATE-SESSION-TITLE",
                created_at=NOW,
                updated_at=NOW,
                round_count=2,
                size_bytes=200,
                preview="PRIVATE-SESSION-PREVIEW",
                has_compaction=True,
            ),
            SessionSummary(
                session_id=branch_id,
                title="PRIVATE-BRANCH-TITLE",
                created_at=NOW + timedelta(seconds=1),
                updated_at=NOW + timedelta(seconds=2),
                round_count=2,
                size_bytes=200,
                preview="PRIVATE-BRANCH-PREVIEW",
                parent_session_id=root_id,
            ),
        ),
        checkpoints=(
            FileTaskCheckpoint(
                checkpoint_id="checkpoint-safe-id",
                created_at=NOW + timedelta(seconds=3),
                edits=(
                    FileEditCheckpoint(
                        path="PRIVATE-CHECKPOINT-PATH",
                        original_content="PRIVATE-CHECKPOINT-BODY",
                        resulting_hash="PRIVATE-CHECKPOINT-HASH",
                        resulting_chars=24,
                    ),
                ),
            ),
        ),
    )


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
    simulation = build_context_what_if(
        _context_snapshot(),
        _context_snapshot("x" * 110_000),
        additional_chars=110_000,
    )

    layers = format_context_layers(context)
    compact_layers = format_context_layers(context, compact=True)
    insights = format_context_insights(context)
    compact_insights = format_context_insights(context, compact=True)
    simulated_insights = format_context_insights(
        _context_snapshot(),
        simulation,
        compact=True,
    )
    detail = format_context_detail(context, simulation)

    assert "SYSTEM FIXED" in layers.plain
    assert "CURRENT CHAIN" in compact_layers.plain
    assert "LARGEST TOOL RESULTS · BODY HIDDEN" in insights.plain
    assert "#01" in insights.plain
    assert "SERVER HIST" in compact_insights.plain
    assert "4.0kc/~1.2kt" in compact_insights.plain
    assert "WHAT-IF" in simulated_insights.plain
    assert "OVER" in simulated_insights.plain
    assert "CHAR" in detail.plain
    assert "LOCAL WHAT-IF · NO MODEL CALL" in detail.plain
    assert "+110,000 INPUT CHARS" in detail.plain
    assert "LAST SERVER MEASUREMENT" in detail.plain
    assert "HISTORICAL · NOT A FORECAST" in detail.plain


def test_security_formatters_show_four_states_and_distinct_layers() -> None:
    security = _security_snapshot()
    watch = project_security_boundary_watch((security,))

    title = format_security_title(security, boundary_watch=watch)
    bands = format_security_capabilities(security)
    compact = format_security_capabilities(security, compact=True)
    short = format_security_capabilities(security, compact=True, short=True)
    compact_watch = format_security_boundary_watch(watch, compact=True)
    detail = format_security_boundaries(security, watch)

    assert "DIRECT 5" in title.plain
    assert "APPROVAL 3" in title.plain
    assert "FORBIDDEN 1" in title.plain
    assert "UNAVAILABLE 1" in title.plain
    assert "ALERT 1" in title.plain
    assert "WORKSPACE READ" in bands.plain
    assert "bounded paths" in bands.plain
    assert "bounded paths" not in compact.plain
    assert len(short.plain.splitlines()) == 9
    assert "FORBIDDEN HOST SHELL" in short.plain
    assert "UNAVAILABLE OS CMD" in short.plain
    assert "P APP" in compact_watch.plain
    assert "N ABS" in compact_watch.plain
    assert "C FIX" in compact_watch.plain
    assert "A REC" in compact_watch.plain
    assert "BOUNDARY WATCH" in detail.plain
    assert "PATH" in detail.plain
    assert "NETWORK" in detail.plain
    assert "COMMAND" in detail.plain
    assert "AUDIT" in detail.plain
    assert "STABLE IN OBSERVATION WINDOW" in detail.plain
    assert "BOUNDED ALERTS" in detail.plain
    assert "APP ALLOWLIST ENFORCED" in detail.plain
    assert (
        "OS PROBE FAILED · FAIL CLOSED" in detail.plain or "OS DISABLED" in detail.plain
    )
    assert "APP ≠ OS" in detail.plain
    assert "LAYER SPLIT" in detail.plain


def test_approval_trace_formatters_link_dag_and_flag_changed_binding() -> None:
    graph = ExecutionGraphProjector().project(
        _approval_execution_events(binding="changed")
    )
    flow = ApprovalFlowProjector().project(graph)
    trace = flow.traces[0]
    approval_node = graph.node(trace.correlation_id)
    assert approval_node is not None

    formatted = format_approval_flows(flow)
    compact = format_approval_flows(flow, compact=True)
    label = format_node_label(approval_node, approval_trace=trace)
    detail = format_node_detail(approval_node, approval_trace=trace)

    assert "APPROVAL FLOW" in formatted.plain
    assert "APPROVED · BINDING CHANGED" in formatted.plain
    assert "tool-eeeeeeee" in formatted.plain
    assert "PREVIEW BODY HIDDEN" in formatted.plain
    assert "APPROVED/CHANGED" in compact.plain
    assert "APPROVED/CHANGED" in label.plain
    assert "APPROVAL TRACE" in detail.plain
    assert "BINDING CHANGED" in detail.plain
    assert "320 CHARS · BODY HIDDEN" in detail.plain
    assert "approval_decision" not in detail.plain
    assert trace.correlation_id in visible_node_ids(graph, "tools")


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
async def test_live_app_runs_and_clears_context_what_if_without_agent_turn() -> None:
    bus = EventBus()
    agent = FakeLiveAgent(bus)
    app = LiveCockpitApp(
        agent,
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.check_action("context_what_if", ()) is False

        await pilot.press("f3")
        await pilot.press("f4")
        await pilot.pause()

        assert isinstance(app.screen, ContextWhatIfScreen)
        what_if_input = app.screen.query_one("#what-if-input", Input)
        assert what_if_input.has_focus
        what_if_input.value = "110000"
        await pilot.press("enter")
        await pilot.pause()

        assert app.context_simulation is not None
        assert app.context_simulation.additional_chars == 110_000
        assert app.context_simulation.projected_pressure.level == "exceeded"
        assert agent.what_if_inputs == [110_000]
        assert agent.prompts == []
        detail = app.query_one("#context-detail").render().plain
        assert "LOCAL WHAT-IF · NO MODEL CALL" in detail
        assert "+110,000 INPUT CHARS" in detail

        await pilot.press("f4")
        await pilot.pause()
        clear_input = app.screen.query_one("#what-if-input", Input)
        clear_input.value = "0"
        await pilot.press("enter")
        await pilot.pause()

        assert app.context_simulation is None
        assert agent.what_if_inputs == [110_000]
        assert app.query_one("#prompt", Input).has_focus

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
async def test_live_app_toggles_security_shield_without_disturbing_primary_view() -> (
    None
):
    bus = EventBus()
    app = LiveCockpitApp(
        FakeLiveAgent(bus),
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
        security=_security_snapshot(),
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        switcher = app.query_one("#workspace", ContentSwitcher)

        await pilot.press("f3")
        await pilot.press("f5")
        await pilot.pause()

        assert app.monitor_view == "security"
        assert switcher.current == "security-view"
        assert (
            "WORKSPACE READ" in app.query_one("#security-capabilities").render().plain
        )
        assert "BOUNDARY WATCH" in app.query_one("#security-detail").render().plain
        assert "NO APPROVAL REQUESTS" in app.query_one("#approval-flows").render().plain
        assert app.check_action("filter_tools", ()) is False
        assert app.check_action("context_what_if", ()) is False
        assert app.query_one("#prompt", Input).has_focus

        await pilot.press("f5")
        await pilot.pause()

        assert app.monitor_view == "context"
        assert switcher.current == "context-view"

        await pilot.press("f5")
        await pilot.resize_terminal(60, 24)
        await pilot.pause()

        assert app.monitor_view == "security"
        assert app.query_one("#security-panel").display
        assert not app.query_one("#security-detail-panel").display
        assert not app.query_one("#security-title").display
        assert "APP ENFORCED · APPROVAL NONE" in str(
            app.query_one("#security-panel").border_title
        )
        assert "P APP · N ABS · C FIX · A REC · Δ0/W1" in str(
            app.query_one("#security-panel").border_subtitle
        )
        assert app.query_one("#transcript", Log).region.height >= 5

    assert bus.close()


@pytest.mark.asyncio
async def test_live_app_browses_time_machine_without_model_or_source_content() -> None:
    bus = EventBus()
    agent = FakeLiveAgent(bus)
    history_calls: list[str] = []

    def history_provider() -> TimeMachineHistory:
        history_calls.append("read")
        return _time_machine_history()

    app = LiveCockpitApp(
        agent,
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
        initial_events=_execution_events(),
        time_machine_history_provider=history_provider,
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("f6")
        await pilot.pause()

        switcher = app.query_one("#workspace", ContentSwitcher)
        tree = app.query_one("#time-machine-tree", Tree)
        assert app.monitor_view == "time-machine"
        assert switcher.current == "time-machine-view"
        assert history_calls == ["read"]
        assert agent.prompts == []
        assert len(tree.root.children) == 3
        assert len(tree.root.children[0].children) == len(_execution_events())
        assert len(tree.root.children[1].children) == 2
        assert len(tree.root.children[2].children) == 1
        assert "READ ONLY" in app.query_one("#time-machine-title").render().plain

        tree.select_node(tree.root.children[0].children[0])
        await pilot.pause()
        assert app.time_machine_snapshot.cursor_sequence == 1
        assert "1/4" in tree.root.label.plain

        tree.select_node(tree.root.children[1].children[1])
        await pilot.pause()
        detail = app.query_one("#time-machine-detail").render().plain
        assert "BRANCH" in detail
        assert "MESSAGE BODIES HIDDEN" in detail
        retained_history = repr(app._time_machine_history)
        for canary in (
            "PRIVATE-SESSION-TITLE",
            "PRIVATE-SESSION-PREVIEW",
            "PRIVATE-BRANCH-TITLE",
            "PRIVATE-BRANCH-PREVIEW",
            "PRIVATE-CHECKPOINT-PATH",
            "PRIVATE-CHECKPOINT-BODY",
            "PRIVATE-CHECKPOINT-HASH",
        ):
            assert canary not in detail
            assert canary not in repr(tree.root)
            assert canary not in retained_history

        await pilot.resize_terminal(60, 24)
        await pilot.pause()
        assert app.query_one("#time-machine-panel").display
        assert not app.query_one("#time-machine-detail-panel").display
        assert app.query_one("#time-machine-inline-detail").display
        assert (
            "MESSAGE BODIES HIDDEN"
            in app.query_one("#time-machine-inline-detail").render().plain
        )
        assert app.query_one("#transcript", Log).region.height >= 5

        await pilot.press("f6")
        await pilot.pause()
        assert app.monitor_view == "execution"
        assert agent.prompts == []

    assert bus.close()


@pytest.mark.asyncio
async def test_live_app_browses_neural_map_without_file_bodies() -> None:
    bus = EventBus()
    agent = FakeLiveAgent(bus)
    app = LiveCockpitApp(
        agent,
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
        initial_events=build_neural_map_fixture_events(),
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("f7")
        await pilot.pause()

        switcher = app.query_one("#workspace", ContentSwitcher)
        tree = app.query_one("#neural-map-tree", Tree)
        assert app.monitor_view == "neural-map"
        assert switcher.current == "neural-map-view"
        assert agent.prompts == []
        assert len(tree.root.children) >= 3
        assert "NEURAL MAP" in app.query_one("#neural-map-title").render().plain
        assert format_neural_map_title(app.neural_map_snapshot).plain

        tree.select_node(tree.root.children[0])
        await pilot.pause()
        detail = app.query_one("#neural-map-detail").render().plain
        assert "METADATA ONLY" in detail
        assert "PRIVATE" not in detail

        await pilot.resize_terminal(60, 24)
        await pilot.pause()
        assert app.query_one("#neural-map-panel").display
        assert not app.query_one("#neural-map-detail-panel").display
        assert app.query_one("#neural-map-inline-detail").display
        assert app.query_one("#transcript", Log).region.height >= 5

        await pilot.press("f7")
        await pilot.pause()
        assert app.monitor_view == "execution"
        assert agent.prompts == []

    assert bus.close()


@pytest.mark.asyncio
async def test_live_app_restores_latest_checkpoint_from_time_machine(
    tmp_path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("before\n", encoding="utf-8")
    tools = FileSystemTools(tmp_path)
    tools.write_file("sample.txt", "after\n")
    latest = tools.checkpoints.latest
    assert latest is not None

    bus = EventBus()
    agent = FakeLiveAgent(bus)

    def history_provider() -> TimeMachineHistory:
        return TimeMachineHistory(checkpoints=tools.checkpoints.snapshots)

    app = LiveCockpitApp(
        agent,
        bus,
        model="deepseek-v4-flash",
        workspace=str(tmp_path),
        initial_events=_execution_events(),
        time_machine_history_provider=history_provider,
        filesystem_tools=tools,
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("f6")
        await pilot.pause()
        tree = app.query_one("#time-machine-tree", Tree)
        checkpoint_node = tree.root.children[2].children[0]
        tree.select_node(checkpoint_node)
        await pilot.pause()
        detail = app.query_one("#time-machine-detail").render().plain
        assert "PRESS R TO PREVIEW" in detail
        app.action_restore_checkpoint()
        await pilot.pause()
        assert isinstance(app.screen, CheckpointRestoreScreen)
        await pilot.click("#checkpoint-restore-approve")
        await pilot.pause()
        assert target.read_text(encoding="utf-8") == "before\n"
        assert tools.checkpoints.count == 0

    assert bus.close()


@pytest.mark.asyncio
async def test_live_security_view_tracks_changed_approval_binding_responsively() -> (
    None
):
    bus = EventBus()
    app = LiveCockpitApp(
        FakeLiveAgent(bus),
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
        security=_security_snapshot(),
        initial_events=_approval_execution_events(binding="changed"),
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("f5")
        await pilot.pause()

        assert app.approval_flow.changed_count == 1
        approval_view = app.query_one("#approval-flows").render().plain
        assert "write_file" in approval_view
        assert "APPROVED · BINDING CHANGED" in approval_view
        assert "tool-eeeeeeee" in approval_view

        await pilot.resize_terminal(80, 28)
        await pilot.pause()

        assert not app.query_one("#approval-flows").display
        assert "APP ENFORCED · APPROVAL APPROVED/CHANGED" in str(
            app.query_one("#security-panel").border_title
        )
        assert "P APP · N ABS · C FIX · A REC · Δ0/W1" in str(
            app.query_one("#security-panel").border_subtitle
        )
        assert app.query_one("#transcript", Log).region.height >= 5

    assert bus.close()


@pytest.mark.asyncio
async def test_live_security_view_reobserves_and_bounds_boundary_changes() -> None:
    bus = EventBus()
    observations = iter((_changed_security_snapshot(),))
    app = LiveCockpitApp(
        FakeLiveAgent(bus),
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
        security=_security_snapshot(),
        security_observer=lambda: next(observations),
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("f5")
        await pilot.pause()

        assert app.security_snapshot.audit_status == "disabled"
        assert app.boundary_watch.observation_count == 2
        assert app.boundary_watch.total_change_count == 3
        assert app.boundary_watch.warning_count == 3
        detail = app.query_one("#security-detail").render().plain
        assert "RECENT CHANGES" in detail
        assert "#2 AUDIT" in detail
        assert "AUDIT  REC → OFF" in detail
        assert "OS FAIL-CLOSED · APP GUARDS ACTIVE" in detail
        assert "AUDIT RECORDING OFF" in detail

        await pilot.resize_terminal(60, 40)
        await pilot.pause()

        compact = app.query_one("#security-watch").render().plain
        assert app.query_one("#security-watch").display
        assert "P APP" in compact
        assert "N ABS" in compact
        assert "C FIX" in compact
        assert "A OFF" in compact
        assert "Δ3" in compact

        await pilot.resize_terminal(80, 28)
        await pilot.pause()

        assert not app.query_one("#security-watch").display
        assert "P APP · N ABS · C FIX · A OFF · Δ3/W3" in str(
            app.query_one("#security-panel").border_subtitle
        )
        assert app.query_one("#transcript", Log).region.height >= 5

    assert bus.close()


@pytest.mark.asyncio
async def test_live_security_observer_failure_keeps_last_safe_snapshot() -> None:
    bus = EventBus()

    def fail_observation() -> SecurityShield:
        raise RuntimeError("PRIVATE-CANARY")

    initial = _security_snapshot()
    app = LiveCockpitApp(
        FakeLiveAgent(bus),
        bus,
        model="deepseek-v4-flash",
        workspace="D:/workspace",
        security=initial,
        security_observer=fail_observation,
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("f5")
        await pilot.pause()

        assert app.security_snapshot is initial
        assert app.boundary_watch.observation_failures == 1
        assert app.boundary_watch.alerts[-1].code == "observation_failed"
        detail = app.query_one("#security-detail").render().plain
        assert "LAST SAFE SNAPSHOT" in detail
        assert "PRIVATE-CANARY" not in detail

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
