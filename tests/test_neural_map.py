"""Tests for the metadata-only Neural Map workspace activity projection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from neil_agent.events import RuntimeEvent, RuntimeMetadataItem, RuntimeStatus
from neil_agent.neural_map import (
    MAX_NEURAL_MAP_NODES,
    NeuralMapProjector,
    build_neural_map_fixture_events,
    classify_activity_kind,
    extract_workspace_paths,
    fold_workspace_path_for_web,
    project_web_runtime_metadata,
    render_neural_map_snapshot,
    sanitize_workspace_path,
)
from neil_agent.schemas import ToolCall

NOW = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)


def _tool_call_events(
    number: int,
    *,
    tool_name: str,
    activity_kind: str,
    workspace_path: str | None,
    status: RuntimeStatus = "succeeded",
    argument_count: int = 1,
) -> tuple[RuntimeEvent, RuntimeEvent]:
    correlation_id = f"tool-{number:032x}"
    start_metadata: list[RuntimeMetadataItem] = [
        RuntimeMetadataItem(name="tool_name", value=tool_name),
        RuntimeMetadataItem(name="argument_count", value=argument_count),
        RuntimeMetadataItem(name="requires_approval", value=False),
        RuntimeMetadataItem(name="activity_kind", value=activity_kind),
    ]
    if workspace_path is not None:
        start_metadata.append(
            RuntimeMetadataItem(name="workspace_path", value=workspace_path)
        )
    started = RuntimeEvent(
        event_id=f"evt-{number * 2:032x}",
        correlation_id=correlation_id,
        parent_event_id=None,
        timestamp=NOW + timedelta(seconds=number),
        stage="tool_call",
        status="started",
        metadata=tuple(start_metadata),
    )
    finished = RuntimeEvent(
        event_id=f"evt-{number * 2 + 1:032x}",
        correlation_id=correlation_id,
        parent_event_id=started.event_id,
        timestamp=NOW + timedelta(seconds=number, milliseconds=500),
        stage="tool_call",
        status=status,
        metadata=(
            RuntimeMetadataItem(name="is_error", value=status == "failed"),
            RuntimeMetadataItem(name="result_chars", value=12),
            RuntimeMetadataItem(name="elapsed_ms", value=8),
        ),
    )
    return started, finished


def test_neural_map_fixture_projects_directory_heat_and_risk() -> None:
    snapshot = NeuralMapProjector().project(build_neural_map_fixture_events())

    assert snapshot.total_activities == 5
    src_node = snapshot.node("src")
    assert src_node is not None
    assert src_node.read_heat == 1
    src_pkg = snapshot.node("src/neil_agent")
    assert src_pkg is not None
    assert src_pkg.read_heat == 1
    assert src_pkg.write_heat == 1
    tests_node = snapshot.node("tests")
    assert tests_node is not None
    assert tests_node.write_heat == 1
    assert tests_node.risk == "high"
    root_node = snapshot.node(".")
    assert root_node is not None
    assert root_node.check_heat == 1
    rendered = render_neural_map_snapshot(snapshot, selection="src")
    assert "NEIL NEURAL MAP" in rendered
    assert "src" in rendered


def test_neural_map_aggregates_git_stage_paths_and_time_windows() -> None:
    events: list[RuntimeEvent] = []
    for index, path in enumerate(("src/a.py", "src/b.py", "docs/readme.md"), start=1):
        events.extend(
            _tool_call_events(
                index,
                tool_name="git_stage",
                activity_kind="check",
                workspace_path=path,
                argument_count=1,
            )
        )
    snapshot = NeuralMapProjector().project(tuple(events))

    assert snapshot.node("src") is not None
    assert snapshot.node("docs") is not None


def test_neural_map_rolls_up_when_directory_limit_exceeded() -> None:
    events: list[RuntimeEvent] = []
    for index in range(MAX_NEURAL_MAP_NODES + 8):
        events.extend(
            _tool_call_events(
                index + 1,
                tool_name="read_file",
                activity_kind="read",
                workspace_path=f"pkg/dir{index}/file.py",
            )
        )
    snapshot = NeuralMapProjector().project(tuple(events))

    assert len(snapshot.nodes) <= MAX_NEURAL_MAP_NODES
    assert snapshot.truncated
    assert snapshot.rolled_up_directories > 0


def test_workspace_path_sanitization_rejects_absolute_and_traversal() -> None:
    assert sanitize_workspace_path("src/foo.py") == "src/foo.py"
    assert sanitize_workspace_path("/etc/passwd") is None
    assert sanitize_workspace_path("src/../secret.py") is None
    assert sanitize_workspace_path("C:\\src\\foo.py") is None


def test_fold_workspace_path_for_web_drops_leaf_filenames() -> None:
    assert (
        fold_workspace_path_for_web(
            "src/neil_agent/agent.py",
            tool_name="read_file",
            activity_kind="read",
        )
        == "src/neil_agent"
    )
    assert (
        fold_workspace_path_for_web(
            "README.md",
            tool_name="read_file",
            activity_kind="read",
        )
        == "."
    )
    assert (
        fold_workspace_path_for_web(
            "src/neil_agent",
            tool_name="list_directory",
            activity_kind="read",
        )
        == "src/neil_agent"
    )
    assert (
        fold_workspace_path_for_web(
            "src/a.py;src/b.py;pkg/c.py",
            tool_name="git_stage",
            activity_kind="write",
        )
        == "src;pkg"
    )
    assert fold_workspace_path_for_web("/etc/passwd", tool_name="read_file") is None


def test_project_web_runtime_metadata_folds_workspace_path() -> None:
    projected = project_web_runtime_metadata(
        {
            "tool_name": "read_file",
            "activity_kind": "read",
            "workspace_path": "src/neil_agent/agent.py",
            "parent_run_id": "run-" + "a" * 32,
        }
    )
    assert projected["workspace_path"] == "src/neil_agent"
    assert projected["parent_run_id"].startswith("run-")
    stripped = project_web_runtime_metadata(
        {"tool_name": "read_file", "workspace_path": "/etc/passwd"}
    )
    assert "workspace_path" not in stripped


def test_tool_activity_metadata_helpers() -> None:
    assert classify_activity_kind("write_file") == "write"
    assert classify_activity_kind("read_file") == "read"
    assert classify_activity_kind("git_status") == "check"
    paths = extract_workspace_paths(
        "git_stage",
        {"paths": ["src/a.py", "src/b.py"]},
    )
    assert paths == ("src/a.py", "src/b.py")
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "src/main.py"})
    assert extract_workspace_paths(call.name, call.arguments) == ("src/main.py",)


def test_neural_map_renderer_rejects_invalid_snapshot_type() -> None:
    with pytest.raises(TypeError, match="NeuralMapSnapshot"):
        render_neural_map_snapshot(object())  # type: ignore[arg-type]
