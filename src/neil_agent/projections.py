"""Deterministic execution-graph and timeline projections from runtime events."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from .events import (
    RuntimeEvent,
    RuntimeMetadataItem,
    RuntimeStage,
    RuntimeStatus,
)

RUNTIME_PROJECTION_VERSION: Literal[1] = 1
DEFAULT_REPLAY_EVENT_LIMIT = 200
DEFAULT_REPLAY_NODE_LIMIT = 200

ProjectionAnomalyCode = Literal[
    "conflicting_event_id",
    "conflicting_terminal_status",
    "cycle_parent_removed",
    "duplicate_event",
    "finish_before_start",
    "missing_parent_event",
    "missing_start",
    "multiple_start_events",
    "parent_not_start_event",
    "repeated_state",
    "self_parent",
    "terminal_parent_mismatch",
]

_INITIAL_STATUSES = frozenset({"started", "waiting"})
_TERMINAL_STATUSES = frozenset({"succeeded", "skipped", "failed"})
_TERMINAL_PRECEDENCE: dict[RuntimeStatus, int] = {
    "started": 0,
    "waiting": 1,
    "succeeded": 2,
    "skipped": 3,
    "failed": 4,
}
_STAGE_ORDER: dict[RuntimeStage, int] = {
    "agent_turn": 0,
    "model_request": 1,
    "tool_call": 2,
    "approval": 3,
    "quality_check": 4,
}


@dataclass(frozen=True, slots=True)
class ProjectionAnomaly:
    """One stable projection warning with no event content."""

    code: ProjectionAnomalyCode
    subject_id: str
    count: int = 1


@dataclass(frozen=True, slots=True)
class ExecutionNode:
    """One operation reconstructed from correlated runtime facts."""

    correlation_id: str
    stage: RuntimeStage
    status: RuntimeStatus
    parent_correlation_id: str | None
    unresolved_parent_event_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    start_metadata: tuple[RuntimeMetadataItem, ...]
    finish_metadata: tuple[RuntimeMetadataItem, ...]
    event_ids: tuple[str, ...]
    anomalies: tuple[ProjectionAnomalyCode, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionEdge:
    """One parent-to-child operation relationship."""

    parent_correlation_id: str
    child_correlation_id: str


@dataclass(frozen=True, slots=True)
class ExecutionGraph:
    """Immutable DAG projection with deterministic ordering."""

    version: Literal[1]
    input_event_count: int
    unique_event_count: int
    nodes: tuple[ExecutionNode, ...]
    edges: tuple[ExecutionEdge, ...]
    anomalies: tuple[ProjectionAnomaly, ...] = ()

    def node(self, correlation_id: str) -> ExecutionNode | None:
        return next(
            (node for node in self.nodes if node.correlation_id == correlation_id),
            None,
        )


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One canonical event in replay order."""

    sequence: int
    event_id: str
    correlation_id: str
    parent_event_id: str | None
    timestamp: datetime
    stage: RuntimeStage
    status: RuntimeStatus
    metadata: tuple[RuntimeMetadataItem, ...]


@dataclass(frozen=True, slots=True)
class RuntimeTimeline:
    """Immutable chronological projection of unique event facts."""

    version: Literal[1]
    input_event_count: int
    unique_event_count: int
    entries: tuple[TimelineEntry, ...]
    anomalies: tuple[ProjectionAnomaly, ...] = ()


@dataclass(frozen=True, slots=True)
class _NormalizedEvents:
    input_event_count: int
    events: tuple[RuntimeEvent, ...]
    anomalies: tuple[ProjectionAnomaly, ...]


@dataclass(slots=True)
class _NodeDraft:
    correlation_id: str
    stage: RuntimeStage
    status: RuntimeStatus
    parent_correlation_id: str | None
    unresolved_parent_event_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    start_metadata: tuple[RuntimeMetadataItem, ...]
    finish_metadata: tuple[RuntimeMetadataItem, ...]
    event_ids: tuple[str, ...]
    anomalies: set[ProjectionAnomalyCode] = field(default_factory=set)


class ExecutionGraphProjector:
    """Build a DAG from events without maintaining parallel Agent state."""

    def project(self, events: Iterable[RuntimeEvent]) -> ExecutionGraph:
        normalized = _normalize_events(events)
        events_by_id = {event.event_id: event for event in normalized.events}
        grouped: dict[str, list[RuntimeEvent]] = defaultdict(list)
        for event in normalized.events:
            grouped[event.correlation_id].append(event)

        drafts = {
            correlation_id: self._node_draft(
                correlation_id,
                correlated_events,
                events_by_id,
            )
            for correlation_id, correlated_events in grouped.items()
        }
        self._break_parent_cycles(drafts)

        nodes = tuple(
            self._freeze_node(draft)
            for draft in sorted(drafts.values(), key=_draft_sort_key)
        )
        edges = tuple(
            sorted(
                (
                    ExecutionEdge(
                        parent_correlation_id=node.parent_correlation_id,
                        child_correlation_id=node.correlation_id,
                    )
                    for node in nodes
                    if node.parent_correlation_id is not None
                ),
                key=lambda edge: (
                    edge.parent_correlation_id,
                    edge.child_correlation_id,
                ),
            )
        )
        node_anomalies = tuple(
            ProjectionAnomaly(code=code, subject_id=node.correlation_id)
            for node in nodes
            for code in node.anomalies
        )
        anomalies = tuple(
            sorted(
                (*normalized.anomalies, *node_anomalies),
                key=_anomaly_sort_key,
            )
        )
        return ExecutionGraph(
            version=RUNTIME_PROJECTION_VERSION,
            input_event_count=normalized.input_event_count,
            unique_event_count=len(normalized.events),
            nodes=nodes,
            edges=edges,
            anomalies=anomalies,
        )

    @staticmethod
    def _node_draft(
        correlation_id: str,
        events: list[RuntimeEvent],
        events_by_id: dict[str, RuntimeEvent],
    ) -> _NodeDraft:
        ordered = sorted(events, key=_event_sort_key)
        stage = ordered[0].stage
        anomalies: set[ProjectionAnomalyCode] = set()

        starts = [event for event in ordered if event.status in _INITIAL_STATUSES]
        if len({event.status for event in ordered}) < len(ordered):
            anomalies.add("repeated_state")
        if starts:
            start = starts[0]
            if len(starts) > 1:
                anomalies.add("multiple_start_events")
        else:
            start = None
            anomalies.add("missing_start")

        terminals = [event for event in ordered if event.status in _TERMINAL_STATUSES]
        if terminals:
            terminal_statuses = {event.status for event in terminals}
            if len(terminal_statuses) > 1:
                anomalies.add("conflicting_terminal_status")
            chosen_status = max(
                terminal_statuses,
                key=lambda status: _TERMINAL_PRECEDENCE[status],
            )
            chosen_terminals = [
                event for event in terminals if event.status == chosen_status
            ]
            finish = max(chosen_terminals, key=_event_sort_key)
        else:
            finish = None
            chosen_status = start.status if start is not None else ordered[-1].status

        parent_correlation_id: str | None = None
        unresolved_parent_event_id: str | None = None
        if start is not None and start.parent_event_id is not None:
            parent_event = events_by_id.get(start.parent_event_id)
            if parent_event is None:
                unresolved_parent_event_id = start.parent_event_id
                anomalies.add("missing_parent_event")
            elif parent_event.correlation_id == correlation_id:
                anomalies.add("self_parent")
            else:
                parent_correlation_id = parent_event.correlation_id
                if parent_event.status not in _INITIAL_STATUSES:
                    anomalies.add("parent_not_start_event")
        elif (
            start is None and finish is not None and finish.parent_event_id is not None
        ):
            if finish.parent_event_id in events_by_id:
                anomalies.add("terminal_parent_mismatch")
            else:
                unresolved_parent_event_id = finish.parent_event_id
                anomalies.add("missing_parent_event")

        if start is not None:
            for terminal in terminals:
                if terminal.parent_event_id != start.event_id:
                    anomalies.add("terminal_parent_mismatch")
                    break
        if (
            start is not None
            and finish is not None
            and finish.timestamp < start.timestamp
        ):
            anomalies.add("finish_before_start")

        return _NodeDraft(
            correlation_id=correlation_id,
            stage=stage,
            status=chosen_status,
            parent_correlation_id=parent_correlation_id,
            unresolved_parent_event_id=unresolved_parent_event_id,
            started_at=None if start is None else start.timestamp,
            finished_at=None if finish is None else finish.timestamp,
            start_metadata=() if start is None else start.metadata,
            finish_metadata=() if finish is None else finish.metadata,
            event_ids=tuple(event.event_id for event in ordered),
            anomalies=anomalies,
        )

    @staticmethod
    def _break_parent_cycles(drafts: dict[str, _NodeDraft]) -> None:
        """Remove one deterministic edge per cycle until the graph is acyclic."""

        while True:
            cycle = _find_parent_cycle(drafts)
            if not cycle:
                return
            breaker = max(cycle)
            drafts[breaker].parent_correlation_id = None
            drafts[breaker].anomalies.add("cycle_parent_removed")

    @staticmethod
    def _freeze_node(draft: _NodeDraft) -> ExecutionNode:
        return ExecutionNode(
            correlation_id=draft.correlation_id,
            stage=draft.stage,
            status=draft.status,
            parent_correlation_id=draft.parent_correlation_id,
            unresolved_parent_event_id=draft.unresolved_parent_event_id,
            started_at=draft.started_at,
            finished_at=draft.finished_at,
            start_metadata=draft.start_metadata,
            finish_metadata=draft.finish_metadata,
            event_ids=draft.event_ids,
            anomalies=tuple(sorted(draft.anomalies)),
        )


class TimelineProjector:
    """Build a canonical time-ordered list from the same event facts."""

    def project(self, events: Iterable[RuntimeEvent]) -> RuntimeTimeline:
        normalized = _normalize_events(events)
        entries = tuple(
            TimelineEntry(
                sequence=index,
                event_id=event.event_id,
                correlation_id=event.correlation_id,
                parent_event_id=event.parent_event_id,
                timestamp=event.timestamp,
                stage=event.stage,
                status=event.status,
                metadata=event.metadata,
            )
            for index, event in enumerate(normalized.events, start=1)
        )
        return RuntimeTimeline(
            version=RUNTIME_PROJECTION_VERSION,
            input_event_count=normalized.input_event_count,
            unique_event_count=len(normalized.events),
            entries=entries,
            anomalies=normalized.anomalies,
        )


def render_runtime_replay(
    events: Iterable[RuntimeEvent],
    *,
    max_timeline_entries: int = DEFAULT_REPLAY_EVENT_LIMIT,
    max_graph_nodes: int = DEFAULT_REPLAY_NODE_LIMIT,
) -> str:
    """Render bounded plain text without Rich, Textual, model, or tool calls."""

    if max_timeline_entries < 1:
        raise ValueError("timeline replay limit must be at least 1")
    if max_graph_nodes < 1:
        raise ValueError("graph replay limit must be at least 1")
    materialized = tuple(events)
    timeline = TimelineProjector().project(materialized)
    graph = ExecutionGraphProjector().project(materialized)
    lines = [
        f"NEIL RUNTIME REPLAY v{RUNTIME_PROJECTION_VERSION}",
        (
            f"events {timeline.input_event_count} input / "
            f"{timeline.unique_event_count} unique | "
            f"nodes {len(graph.nodes)} | edges {len(graph.edges)} | "
            f"anomalies {len(graph.anomalies)}"
        ),
        "",
        "TIMELINE",
    ]
    selected_entries, omitted_entries = _bounded_items(
        timeline.entries,
        max_timeline_entries,
    )
    for entry in selected_entries:
        if entry is None:
            lines.append(f"... {omitted_entries} timeline entries omitted ...")
            continue
        metadata = _format_metadata(entry.metadata)
        suffix = f" | {metadata}" if metadata else ""
        lines.append(
            f"{entry.sequence:04d} {_format_timestamp(entry.timestamp)} "
            f"{entry.stage:<13} {entry.status:<9} "
            f"{_short_id(entry.correlation_id)} "
            f"parent={_short_id(entry.parent_event_id)}{suffix}"
        )

    lines.extend(("", "EXECUTION GRAPH"))
    graph_lines, omitted_nodes = _render_graph_nodes(graph, max_graph_nodes)
    lines.extend(graph_lines or ("(empty)",))
    if omitted_nodes:
        lines.append(f"... {omitted_nodes} graph nodes omitted ...")

    if graph.anomalies:
        lines.extend(("", "ANOMALIES"))
        lines.extend(
            f"- {anomaly.code} {_short_id(anomaly.subject_id)}"
            + (f" x{anomaly.count}" if anomaly.count > 1 else "")
            for anomaly in graph.anomalies
        )
    return "\n".join(lines)


def _normalize_events(events: Iterable[RuntimeEvent]) -> _NormalizedEvents:
    grouped: dict[str, list[RuntimeEvent]] = defaultdict(list)
    input_event_count = 0
    for event in events:
        if not isinstance(event, RuntimeEvent):
            raise TypeError("runtime projectors accept only RuntimeEvent instances")
        canonical = _canonical_event(event)
        grouped[canonical.event_id].append(canonical)
        input_event_count += 1

    unique: list[RuntimeEvent] = []
    anomalies: list[ProjectionAnomaly] = []
    for event_id in sorted(grouped):
        candidates = grouped[event_id]
        serialized = [(_canonical_event_payload(event), event) for event in candidates]
        serialized.sort(key=lambda item: item[0])
        chosen = serialized[0][1]
        unique.append(chosen)
        if len(candidates) == 1:
            continue
        distinct = {payload for payload, _event in serialized}
        code: ProjectionAnomalyCode = (
            "duplicate_event" if len(distinct) == 1 else "conflicting_event_id"
        )
        anomalies.append(
            ProjectionAnomaly(
                code=code,
                subject_id=event_id,
                count=len(candidates) - 1,
            )
        )
    unique.sort(key=_event_sort_key)
    return _NormalizedEvents(
        input_event_count=input_event_count,
        events=tuple(unique),
        anomalies=tuple(sorted(anomalies, key=_anomaly_sort_key)),
    )


def _canonical_event_payload(event: RuntimeEvent) -> str:
    payload = event.model_dump(mode="json")
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _canonical_event(event: RuntimeEvent) -> RuntimeEvent:
    metadata = tuple(sorted(event.metadata, key=lambda item: item.name))
    if metadata == event.metadata:
        return event
    return event.model_copy(update={"metadata": metadata})


def _find_parent_cycle(drafts: dict[str, _NodeDraft]) -> tuple[str, ...]:
    complete: set[str] = set()
    for start in sorted(drafts):
        if start in complete:
            continue
        path: list[str] = []
        positions: dict[str, int] = {}
        current: str | None = start
        while current is not None and current in drafts:
            if current in positions:
                return tuple(path[positions[current] :])
            if current in complete:
                break
            positions[current] = len(path)
            path.append(current)
            current = drafts[current].parent_correlation_id
        complete.update(path)
    return ()


def _render_graph_nodes(
    graph: ExecutionGraph,
    limit: int,
) -> tuple[tuple[str, ...], int]:
    nodes = {node.correlation_id: node for node in graph.nodes}
    children: dict[str, list[ExecutionNode]] = defaultdict(list)
    roots: list[ExecutionNode] = []
    for node in graph.nodes:
        if node.parent_correlation_id is None:
            roots.append(node)
        else:
            children[node.parent_correlation_id].append(node)
    for siblings in children.values():
        siblings.sort(key=_node_sort_key)
    roots.sort(key=_node_sort_key)

    lines: list[str] = []
    stack = [(node, 0) for node in reversed(roots)]
    while stack and len(lines) < limit:
        node, depth = stack.pop()
        details: list[str] = []
        tool_name = _metadata_value(node.start_metadata, "tool_name")
        if tool_name is not None:
            details.append(f"tool={tool_name}")
        if node.unresolved_parent_event_id is not None:
            details.append(
                f"missing-parent={_short_id(node.unresolved_parent_event_id)}"
            )
        if node.anomalies:
            details.append("anomalies=" + ",".join(node.anomalies))
        suffix = f" | {'; '.join(details)}" if details else ""
        lines.append(
            f"{'  ' * depth}- {node.stage} {_short_id(node.correlation_id)} "
            f"[{node.status}] events={len(node.event_ids)}{suffix}"
        )
        stack.extend(
            (child, depth + 1)
            for child in reversed(children.get(node.correlation_id, ()))
        )
    return tuple(lines), max(len(nodes) - len(lines), 0)


def _bounded_items(
    items: tuple[TimelineEntry, ...],
    limit: int,
) -> tuple[tuple[TimelineEntry | None, ...], int]:
    if len(items) <= limit:
        return items, 0
    head = (limit + 1) // 2
    tail = limit // 2
    omitted = len(items) - head - tail
    trailing = items[-tail:] if tail else ()
    return (*items[:head], None, *trailing), omitted


def _format_metadata(metadata: tuple[RuntimeMetadataItem, ...]) -> str:
    return " ".join(f"{item.name}={item.value}" for item in metadata)


def _metadata_value(
    metadata: tuple[RuntimeMetadataItem, ...],
    name: str,
) -> bool | int | str | None:
    return next((item.value for item in metadata if item.name == name), None)


def _event_sort_key(event: RuntimeEvent) -> tuple[datetime, str]:
    return event.timestamp.astimezone(timezone.utc), event.event_id


def _draft_sort_key(draft: _NodeDraft) -> tuple[bool, datetime, int, str]:
    return (
        draft.started_at is None,
        draft.started_at or datetime.max.replace(tzinfo=timezone.utc),
        _STAGE_ORDER[draft.stage],
        draft.correlation_id,
    )


def _node_sort_key(node: ExecutionNode) -> tuple[bool, datetime, int, str]:
    return (
        node.started_at is None,
        node.started_at or datetime.max.replace(tzinfo=timezone.utc),
        _STAGE_ORDER[node.stage],
        node.correlation_id,
    )


def _anomaly_sort_key(
    anomaly: ProjectionAnomaly,
) -> tuple[str, str, int]:
    return anomaly.code, anomaly.subject_id, anomaly.count


def _format_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _short_id(value: str | None) -> str:
    if value is None:
        return "-"
    prefix, _, token = value.partition("-")
    return f"{prefix}-{token[:8]}"
