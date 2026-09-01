"""Bounded, metadata-only Neural Map projections for workspace file activity."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, cast
from unicodedata import category

from .activity import COMMAND_TOOL_NAMES, WRITE_TOOL_NAMES
from .events import (
    MAX_RUNTIME_METADATA_TEXT_CHARS,
    RuntimeEvent,
    RuntimeMetadataItem,
)
from .projections import ExecutionGraphProjector, ExecutionNode
from .schemas import ToolCall

NEURAL_MAP_VERSION: Literal[1] = 1
MAX_NEURAL_MAP_EVENTS = 512
MAX_NEURAL_MAP_NODES = 48
MAX_NEURAL_MAP_TIME_BUCKETS = 3
_TIME_WINDOW_LABELS = ("EARLY", "MID", "LATE")

NeuralMapActivityKind = Literal["read", "write", "check", "other"]
NeuralMapRiskLevel = Literal["low", "medium", "high"]

_PATH_ARG_BY_TOOL: dict[str, str] = {
    "list_directory": "path",
    "read_file": "path",
    "search_text": "path",
    "write_file": "path",
    "replace_text": "path",
}


@dataclass(frozen=True, slots=True)
class NeuralMapTimeBucket:
    """One bounded activity window without file bodies."""

    label: str
    read_heat: int
    write_heat: int
    check_heat: int


@dataclass(frozen=True, slots=True)
class NeuralMapDirectoryNode:
    """One directory cluster with read/write/check heat and risk coloring."""

    directory: str
    read_heat: int
    write_heat: int
    check_heat: int
    risk: NeuralMapRiskLevel
    dominant_window: str


@dataclass(frozen=True, slots=True)
class NeuralMapSnapshot:
    """Immutable workspace activity heat map from runtime events only."""

    version: Literal[1]
    input_event_count: int
    unique_event_count: int
    total_activities: int
    rolled_up_directories: int
    truncated: bool
    time_buckets: tuple[NeuralMapTimeBucket, ...]
    nodes: tuple[NeuralMapDirectoryNode, ...]

    def node(self, directory: str) -> NeuralMapDirectoryNode | None:
        return next(
            (item for item in self.nodes if item.directory == directory),
            None,
        )


class NeuralMapProjector:
    """Project desensitized file-activity metadata into a bounded heat map."""

    def project(self, events: Iterable[RuntimeEvent]) -> NeuralMapSnapshot:
        materialized = tuple(events)[-MAX_NEURAL_MAP_EVENTS:]
        graph = ExecutionGraphProjector().project(materialized)
        activities = _collect_file_activities(graph.nodes)
        if not activities:
            return NeuralMapSnapshot(
                version=NEURAL_MAP_VERSION,
                input_event_count=len(materialized),
                unique_event_count=graph.unique_event_count,
                total_activities=0,
                rolled_up_directories=0,
                truncated=len(materialized) > MAX_NEURAL_MAP_EVENTS,
                time_buckets=_empty_time_buckets(),
                nodes=(),
            )

        directory_stats: dict[str, _DirectoryStats] = defaultdict(_DirectoryStats)
        bucket_stats = {label: _DirectoryStats() for label in _TIME_WINDOW_LABELS}

        for activity in activities:
            for directory in activity.directories:
                directory_stats[directory].add(activity)
            bucket_stats[activity.window_label].add(activity)

        nodes, rolled_up, truncated = _bound_directory_nodes(directory_stats)
        time_buckets = tuple(
            NeuralMapTimeBucket(
                label=label,
                read_heat=bucket_stats[label].read_heat,
                write_heat=bucket_stats[label].write_heat,
                check_heat=bucket_stats[label].check_heat,
            )
            for label in _TIME_WINDOW_LABELS
        )
        return NeuralMapSnapshot(
            version=NEURAL_MAP_VERSION,
            input_event_count=len(materialized),
            unique_event_count=graph.unique_event_count,
            total_activities=len(activities),
            rolled_up_directories=rolled_up,
            truncated=truncated or len(materialized) > MAX_NEURAL_MAP_EVENTS,
            time_buckets=time_buckets,
            nodes=nodes,
        )


@dataclass(slots=True)
class _DirectoryStats:
    read_heat: int = 0
    write_heat: int = 0
    check_heat: int = 0
    failed_writes: int = 0
    window_counts: dict[str, int] = field(default_factory=dict)

    def add(self, activity: _FileActivity) -> None:
        if activity.kind == "read":
            self.read_heat += activity.weight
        elif activity.kind == "write":
            self.write_heat += activity.weight
            if activity.failed:
                self.failed_writes += activity.weight
        elif activity.kind == "check":
            self.check_heat += activity.weight
        self.window_counts[activity.window_label] = (
            self.window_counts.get(activity.window_label, 0) + activity.weight
        )

    @property
    def total_heat(self) -> int:
        return self.read_heat + self.write_heat + self.check_heat

    def dominant_window(self) -> str:
        if not self.window_counts:
            return _TIME_WINDOW_LABELS[0]
        return max(
            _TIME_WINDOW_LABELS,
            key=lambda label: (
                self.window_counts.get(label, 0),
                -_TIME_WINDOW_LABELS.index(label),
            ),
        )

    def risk_level(self) -> NeuralMapRiskLevel:
        if self.write_heat == 0:
            return "low"
        if self.failed_writes > 0 or self.write_heat >= 3:
            return "high"
        return "medium"


@dataclass(frozen=True, slots=True)
class _FileActivity:
    kind: NeuralMapActivityKind
    directories: tuple[str, ...]
    window_label: str
    weight: int
    failed: bool


def tool_activity_metadata(call: ToolCall) -> dict[str, object]:
    """Return bounded metadata fields for Neural Map projections."""

    kind = classify_activity_kind(call.name)
    paths = extract_workspace_paths(call.name, call.arguments)
    metadata: dict[str, object] = {"activity_kind": kind}
    if paths:
        metadata["workspace_path"] = ";".join(paths)
    return metadata


def classify_activity_kind(tool_name: str) -> NeuralMapActivityKind:
    if tool_name in WRITE_TOOL_NAMES:
        return "write"
    if tool_name in {"list_directory", "read_file", "search_text"}:
        return "read"
    if tool_name in COMMAND_TOOL_NAMES:
        return "check"
    return "other"


def extract_workspace_paths(
    tool_name: str,
    arguments: Mapping[str, object],
) -> tuple[str, ...]:
    if tool_name == "git_stage":
        paths = arguments.get("paths")
        if not isinstance(paths, list):
            return ()
        sanitized = tuple(
            path
            for path in (sanitize_workspace_path(item) for item in paths)
            if path is not None
        )
        return _join_bounded_paths(sanitized)
    path_key = _PATH_ARG_BY_TOOL.get(tool_name)
    if path_key is None:
        return ()
    path_value = sanitize_workspace_path(arguments.get(path_key))
    return (path_value,) if path_value is not None else ()


def render_neural_map_snapshot(
    snapshot: NeuralMapSnapshot,
    *,
    selection: str | None = None,
) -> str:
    if not isinstance(snapshot, NeuralMapSnapshot):
        raise TypeError("neural map renderer requires a NeuralMapSnapshot")
    lines = [
        f"NEIL NEURAL MAP v{snapshot.version} · METADATA ONLY",
        (
            f"activities {snapshot.total_activities} · directories "
            f"{len(snapshot.nodes)} · rolled up {snapshot.rolled_up_directories}"
            + (" · TRUNCATED" if snapshot.truncated else "")
        ),
        (
            "windows "
            + " · ".join(
                f"{bucket.label} R{bucket.read_heat}/W{bucket.write_heat}/C{bucket.check_heat}"
                for bucket in snapshot.time_buckets
            )
        ),
    ]
    if not snapshot.nodes:
        lines.append("")
        lines.append("NO FILE ACTIVITY")
        return "\n".join(lines)
    lines.append("")
    for node in snapshot.nodes:
        marker = {"low": "·", "medium": "!", "high": "*"}[node.risk]
        prefix = ">" if selection == node.directory else " "
        lines.append(
            f"{prefix}{marker} {node.directory:<24} "
            f"R{node.read_heat:>3} W{node.write_heat:>3} C{node.check_heat:>3} "
            f"{node.dominant_window}"
        )
    if selection is not None and snapshot.node(selection) is None:
        lines.append("")
        lines.append("DIRECTORY NOT AVAILABLE")
    return "\n".join(lines)


def build_neural_map_fixture_events() -> tuple[RuntimeEvent, ...]:
    """Static fixture for validating Neural Map value without a live workspace."""

    base = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
    events: list[RuntimeEvent] = []

    def add_tool_call(
        index: int,
        *,
        tool_name: str,
        activity_kind: NeuralMapActivityKind,
        workspace_path: str | None,
        status: Literal["succeeded", "failed"] = "succeeded",
    ) -> None:
        correlation_id = f"tool-{index:032x}"
        start_metadata: list[RuntimeMetadataItem] = [
            RuntimeMetadataItem(name="tool_name", value=tool_name),
            RuntimeMetadataItem(name="argument_count", value=1),
            RuntimeMetadataItem(name="requires_approval", value=False),
            RuntimeMetadataItem(name="activity_kind", value=activity_kind),
        ]
        if workspace_path is not None:
            start_metadata.append(
                RuntimeMetadataItem(name="workspace_path", value=workspace_path)
            )
        started = RuntimeEvent(
            event_id=f"evt-{index * 2:032x}",
            correlation_id=correlation_id,
            parent_event_id=None,
            timestamp=base.replace(minute=index),
            stage="tool_call",
            status="started",
            metadata=tuple(start_metadata),
        )
        finish_metadata: list[RuntimeMetadataItem] = [
            RuntimeMetadataItem(name="is_error", value=status == "failed"),
            RuntimeMetadataItem(name="result_chars", value=24),
            RuntimeMetadataItem(name="elapsed_ms", value=12),
        ]
        if status == "failed":
            finish_metadata.append(
                RuntimeMetadataItem(name="error_type", value="ToolError")
            )
        events.append(started)
        events.append(
            RuntimeEvent(
                event_id=f"evt-{index * 2 + 1:032x}",
                correlation_id=correlation_id,
                parent_event_id=started.event_id,
                timestamp=base.replace(minute=index, second=30),
                stage="tool_call",
                status=status,
                metadata=tuple(finish_metadata),
            )
        )

    add_tool_call(
        1,
        tool_name="read_file",
        activity_kind="read",
        workspace_path="src/neil_agent/agent.py",
    )
    add_tool_call(
        2,
        tool_name="write_file",
        activity_kind="write",
        workspace_path="src/neil_agent/neural_map.py",
    )
    add_tool_call(
        3,
        tool_name="search_text",
        activity_kind="read",
        workspace_path="src",
    )
    add_tool_call(
        4,
        tool_name="run_quality_check",
        activity_kind="check",
        workspace_path=None,
    )
    add_tool_call(
        5,
        tool_name="write_file",
        activity_kind="write",
        workspace_path="tests/test_neural_map.py",
        status="failed",
    )
    return tuple(events)


def _collect_file_activities(nodes: Iterable[ExecutionNode]) -> tuple[_FileActivity, ...]:
    pending: list[tuple[datetime, _FileActivity]] = []
    for node in nodes:
        if node.stage != "tool_call" or node.status not in {"succeeded", "failed"}:
            continue
        metadata = _node_metadata(node)
        kind = metadata.get("activity_kind")
        if kind not in {"read", "write", "check"}:
            continue
        timestamp = node.finished_at or node.started_at
        if timestamp is None:
            continue
        paths = _split_workspace_paths(metadata.get("workspace_path"))
        tool_name = str(metadata.get("tool_name", ""))
        directories = tuple(
            _directory_for_path(path, str(kind), tool_name) for path in paths
        ) or (".",)
        raw_count = metadata.get("argument_count", 1)
        count = raw_count if isinstance(raw_count, int) else 1
        pending.append(
            (
                timestamp,
                _FileActivity(
                    kind=kind,  # type: ignore[arg-type]
                    directories=directories,
                    window_label=_TIME_WINDOW_LABELS[0],
                    weight=max(count, 1),
                    failed=bool(metadata.get("is_error")),
                ),
            )
        )
    if not pending:
        return ()
    pending.sort(key=lambda item: (item[0], item[1].directories))
    window_labels = _assign_window_labels([timestamp for timestamp, _ in pending])
    return tuple(
        _FileActivity(
            kind=activity.kind,
            directories=activity.directories,
            window_label=window_labels[index],
            weight=activity.weight,
            failed=activity.failed,
        )
        for index, (_, activity) in enumerate(pending)
    )


def _assign_window_labels(timestamps: list[datetime]) -> list[str]:
    if not timestamps:
        return []
    if len(timestamps) == 1:
        return [_TIME_WINDOW_LABELS[-1]]
    ordered_indices = sorted(range(len(timestamps)), key=lambda index: timestamps[index])
    labels = [""] * len(timestamps)
    for rank, index in enumerate(ordered_indices):
        bucket = min(
            rank * len(_TIME_WINDOW_LABELS) // len(timestamps),
            len(_TIME_WINDOW_LABELS) - 1,
        )
        labels[index] = _TIME_WINDOW_LABELS[bucket]
    return labels


def _empty_time_buckets() -> tuple[NeuralMapTimeBucket, ...]:
    return tuple(
        NeuralMapTimeBucket(label=label, read_heat=0, write_heat=0, check_heat=0)
        for label in _TIME_WINDOW_LABELS
    )


def _bound_directory_nodes(
    directory_stats: Mapping[str, _DirectoryStats],
) -> tuple[tuple[NeuralMapDirectoryNode, ...], int, bool]:
    stats = {directory: value for directory, value in directory_stats.items()}
    rolled_up = 0
    truncated = len(stats) > MAX_NEURAL_MAP_NODES

    while len(stats) > MAX_NEURAL_MAP_NODES:
        candidates = [
            directory
            for directory in stats
            if _parent_directory(directory) != directory
        ]
        if not candidates:
            break
        target = min(
            candidates,
            key=lambda directory: (
                stats[directory].total_heat,
                -directory.count("/"),
                directory,
            ),
        )
        parent = _parent_directory(target)
        rolled_up += 1
        parent_stats = stats.setdefault(parent, _DirectoryStats())
        parent_stats.read_heat += stats[target].read_heat
        parent_stats.write_heat += stats[target].write_heat
        parent_stats.check_heat += stats[target].check_heat
        parent_stats.failed_writes += stats[target].failed_writes
        for label, count in stats[target].window_counts.items():
            parent_stats.window_counts[label] = (
                parent_stats.window_counts.get(label, 0) + count
            )
        del stats[target]

    nodes = tuple(
        NeuralMapDirectoryNode(
            directory=directory,
            read_heat=item.read_heat,
            write_heat=item.write_heat,
            check_heat=item.check_heat,
            risk=item.risk_level(),
            dominant_window=item.dominant_window(),
        )
        for directory, item in sorted(
            stats.items(),
            key=lambda pair: (-pair[1].total_heat, pair[0]),
        )
    )
    return nodes, rolled_up, truncated


def _node_metadata(node: ExecutionNode) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for item in (*node.start_metadata, *node.finish_metadata):
        metadata[item.name] = item.value
    return metadata


def _directory_for_path(
    path: str,
    activity_kind: str,
    tool_name: str,
) -> str:
    normalized = path.replace("\\", "/").strip("/")
    if not normalized:
        return "."
    if tool_name in {"list_directory", "search_text"}:
        return normalized
    if activity_kind == "check" and tool_name not in {"git_stage"}:
        return normalized
    if "/" not in normalized:
        return "."
    return normalized.rsplit("/", 1)[0]


def _parent_directory(directory: str) -> str:
    if directory == ".":
        return "."
    if "/" not in directory:
        return "."
    return directory.rsplit("/", 1)[0]


def _split_workspace_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        return ()
    return tuple(
        segment
        for segment in (part.strip() for part in value.split(";"))
        if segment
    )


def _join_bounded_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    if not paths:
        return ()
    joined = ";".join(paths)
    if len(joined) <= MAX_RUNTIME_METADATA_TEXT_CHARS:
        return paths
    visible: list[str] = []
    length = 0
    for path in paths:
        extra = len(path) if not visible else len(path) + 1
        if length + extra > MAX_RUNTIME_METADATA_TEXT_CHARS:
            break
        visible.append(path)
        length += extra
    return tuple(visible)


def sanitize_workspace_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.replace("\\", "/").strip()
    if not normalized or normalized.startswith("/"):
        return None
    if len(normalized) >= 2 and normalized[1] == ":":
        return None
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        return None
    safe_parts: list[str] = []
    for part in parts:
        safe = "".join(
            character if not category(character).startswith("C") else " "
            for character in part
        )
        safe = " ".join(safe.split())
        if not safe:
            return None
        safe_parts.append(safe)
    path = "/".join(safe_parts)
    if not path:
        return None
    if len(path) > MAX_RUNTIME_METADATA_TEXT_CHARS:
        path = f"{path[: MAX_RUNTIME_METADATA_TEXT_CHARS - 3]}..."
    return path


def fold_workspace_path_for_web(
    value: object,
    *,
    tool_name: str = "",
    activity_kind: str = "other",
) -> str | None:
    """Keep unique relative directories; drop leaf filenames for Web DTOs."""

    kind = (
        cast(NeuralMapActivityKind, activity_kind)
        if activity_kind in {"read", "write", "check", "other"}
        else "other"
    )
    folded: list[str] = []
    seen: set[str] = set()
    for segment in _split_workspace_paths(value):
        sanitized = sanitize_workspace_path(segment)
        if sanitized is None:
            continue
        directory = _directory_for_path(sanitized, kind, tool_name)
        if directory not in seen:
            seen.add(directory)
            folded.append(directory)
    bounded = _join_bounded_paths(tuple(folded))
    if not bounded:
        return None
    return ";".join(bounded)


def project_web_runtime_metadata(
    metadata: Mapping[str, bool | int | str],
) -> dict[str, bool | int | str]:
    """Copy runtime metadata and fold ``workspace_path`` to directories."""

    projected = dict(metadata)
    raw_path = projected.get("workspace_path")
    if raw_path is None:
        return projected
    tool_name = projected.get("tool_name", "")
    activity_kind = projected.get("activity_kind", "other")
    folded = fold_workspace_path_for_web(
        raw_path,
        tool_name=tool_name if isinstance(tool_name, str) else "",
        activity_kind=activity_kind if isinstance(activity_kind, str) else "other",
    )
    if folded is None:
        projected.pop("workspace_path", None)
    else:
        projected["workspace_path"] = folded
    return projected
