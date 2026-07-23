"""Tests for the read-only Rich mission-control cockpit."""

from dataclasses import replace
from io import StringIO
from pathlib import Path

from rich.console import Console

from neil_agent.cockpit import CockpitSnapshot, build_cockpit_panel
from neil_agent.context import ContextStats
from neil_agent.schemas import TokenUsage
from neil_agent.task import QualityCheckRecord, TaskStep


def _snapshot(tmp_path: Path) -> CockpitSnapshot:
    return CockpitSnapshot(
        model="deepseek-v4-flash",
        thinking_enabled=False,
        workspace=tmp_path,
        session_id="20260723T120000000000Z-deadbeef",
        session_title="Visualize runtime",
        context=ContextStats(
            budget_chars=120_000,
            fixed_chars=12_000,
            stored_rounds=4,
            stored_messages=10,
            stored_message_chars=35_000,
            selected_rounds=3,
            selected_messages=8,
            selected_message_chars=26_000,
            omitted_rounds=1,
            budget_tokens=64_000,
            fixed_tokens=3_600,
            stored_message_tokens=10_500,
            selected_message_tokens=7_800,
        ),
        last_usage=TokenUsage(input_tokens=8_000, output_tokens=1_200),
        plan=(
            TaskStep("Inspect runtime state", "completed"),
            TaskStep("Build cockpit", "in_progress"),
            TaskStep("Run tests", "pending"),
        ),
        latest_quality_check=QualityCheckRecord(
            check="pytest",
            status="passed",
            command="python -m pytest -q",
            exit_code=0,
            output="passed",
        ),
        tool_count=12,
        approval_tool_count=5,
        instruction_status="active",
        instruction_sources=2,
        instruction_bytes=1_024,
        checkpoint_count=3,
        audit_enabled=True,
        git_branch="## main...origin/main",
        git_changes=4,
    )


def _render(snapshot: CockpitSnapshot, *, width: int) -> str:
    output = StringIO()
    console = Console(
        file=output,
        width=width,
        color_system=None,
        force_terminal=False,
    )
    console.print(build_cockpit_panel(snapshot))
    return output.getvalue()


def test_cockpit_renders_useful_runtime_metadata(tmp_path: Path) -> None:
    output = _render(_snapshot(tmp_path), width=110)

    assert "NEIL // MISSION CONTROL" in output
    assert "TASK MATRIX" in output
    assert "CONTEXT TOMOGRAPHY" in output
    assert "SECURITY SHIELD" in output
    assert "WORKSPACE SIGNAL" in output
    assert "Build cockpit" in output
    assert "TOTAL 9,200" in output
    assert "DIRECT 7 · APPROVAL 5" in output
    assert "RECORDING METADATA" in output
    assert "3 IN-MEMORY" in output
    assert "4 CHANGES" in output


def test_cockpit_remains_readable_in_narrow_terminal(tmp_path: Path) -> None:
    output = _render(_snapshot(tmp_path), width=52)

    assert "MISSION CONTROL" in output
    assert "TASK MATRIX" in output
    assert "CONTEXT" in output
    assert "SECURITY" in output
    assert "/permissions" in output


def test_cockpit_treats_runtime_values_as_plain_bounded_text(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    unsafe = replace(
        snapshot,
        git_branch="## [bold red]main[/bold red]\x1b[31m",
    )

    output = _render(unsafe, width=110)

    assert "[bold red]main[/bold red]" in output
    assert "\x1b" not in output
