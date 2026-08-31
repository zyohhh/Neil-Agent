"""Tests for the deterministic, metadata-only Security Shield projection."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pytest

from neil_agent.audit import AuditLogStatus
from neil_agent.errors import AuditError
from neil_agent.events import RuntimeEvent, RuntimeEventFactory
from neil_agent.projections import ExecutionGraphProjector
from neil_agent.sandbox import SandboxCapabilities
from neil_agent.security import (
    APPROVAL_FLOW_SCHEMA_VERSION,
    MAX_SECURITY_BOUNDARY_ALERTS,
    MAX_SECURITY_BOUNDARY_CHANGES,
    SECURITY_BOUNDARY_WATCH_SCHEMA_VERSION,
    SECURITY_SHIELD_SCHEMA_VERSION,
    ApprovalFlowProjector,
    SecurityCapability,
    observe_security_shield,
    project_security_boundary_watch,
    project_security_shield,
)


def _tool_permissions() -> dict[str, bool]:
    return {
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
    }


def _approval_events(
    *,
    binding: Literal["valid", "changed"] = "valid",
    tool_name: str = "write_file",
) -> tuple[RuntimeEvent, ...]:
    counter = iter(f"{index:032x}" for index in range(1, 20))
    factory = RuntimeEventFactory(
        clock=lambda: datetime(2026, 8, 2, tzinfo=timezone.utc),
        token_factory=lambda: next(counter),
    )
    tool_id = factory.new_correlation_id("tool_call")
    tool_start = factory.create(
        stage="tool_call",
        status="started",
        correlation_id=tool_id,
        metadata={
            "tool_name": tool_name,
            "argument_count": 2,
            "requires_approval": True,
        },
    )
    approval_id = factory.new_correlation_id("approval")
    approval_start = factory.create(
        stage="approval",
        status="waiting",
        correlation_id=approval_id,
        parent_event_id=tool_start.event_id,
        metadata={
            "tool_name": tool_name,
            "preview_chars": 240,
            "approval_decision": "pending",
            "preview_binding": "pending",
        },
    )
    approval_finish = factory.create(
        stage="approval",
        status="succeeded",
        correlation_id=approval_id,
        parent_event_id=approval_start.event_id,
        metadata={
            "approval_decision": "approved",
            "preview_binding": "pending",
            "elapsed_ms": 12,
        },
    )
    tool_finish = factory.create(
        stage="tool_call",
        status="succeeded" if binding == "valid" else "failed",
        correlation_id=tool_id,
        parent_event_id=tool_start.event_id,
        metadata={
            "approval_decision": "approved",
            "preview_binding": binding,
            "is_error": binding != "valid",
            "result_chars": 20,
            "elapsed_ms": 20,
        },
    )
    return tool_start, approval_start, approval_finish, tool_finish


def test_projection_builds_four_bands_and_keeps_enforcement_layers_distinct() -> None:
    shield = project_security_shield(
        _tool_permissions(),
        sandbox_backend="disabled",
        audit_enabled=True,
    )

    assert shield.schema_version == SECURITY_SHIELD_SCHEMA_VERSION
    assert shield.tool_count == 13
    assert shield.direct_tool_count == 8
    assert shield.approval_tool_count == 5
    assert shield.capability_count("direct") == 4
    assert shield.capability_count("approval") == 3
    assert shield.capability_count("forbidden") == 1
    assert shield.capability_count("unavailable") == 1
    assert shield.application.layer == "application"
    assert shield.application.status == "enforced"
    assert shield.os_sandbox.layer == "os"
    assert shield.os_sandbox.status == "disabled"
    assert shield.audit_status == "recording"
    assert "not a sandbox" in " ".join(shield.os_sandbox.details)


def test_boundary_watch_projects_four_fixed_value_free_signals() -> None:
    security = project_security_shield(
        _tool_permissions(),
        sandbox_backend="disabled",
        audit_enabled=True,
    )

    watch = project_security_boundary_watch((security,))

    assert watch.schema_version == SECURITY_BOUNDARY_WATCH_SCHEMA_VERSION
    assert watch.observation_count == 1
    assert watch.total_change_count == 0
    assert tuple(signal.key for signal in watch.signals) == (
        "path",
        "network",
        "command",
        "audit",
    )
    assert watch.signal("path").state == "application_only"
    assert watch.signal("path").qualifier == "os_disabled"
    assert watch.signal("network").state == "absent"
    assert watch.signal("command").state == "restricted"
    assert watch.signal("audit").state == "recording"
    assert [(alert.scope, alert.code) for alert in watch.alerts] == [
        ("path", "os_disabled")
    ]


def test_boundary_watch_tracks_adjacent_changes_and_downgrades() -> None:
    initial = project_security_shield(
        _tool_permissions(),
        sandbox_backend="disabled",
        audit_enabled=True,
    )
    changed = project_security_shield(
        _tool_permissions(),
        sandbox_backend="windows-sandbox",
        audit_enabled=False,
        sandbox_probe_failed=True,
    )

    watch = project_security_boundary_watch((initial, changed))

    assert watch.observation_count == 2
    assert watch.total_change_count == 3
    assert [change.after.key for change in watch.changes] == [
        "path",
        "network",
        "audit",
    ]
    assert watch.changes[-1].severity == "warning"
    assert watch.signal("path").qualifier == "os_fail_closed"
    assert watch.signal("audit").state == "disabled"
    assert {alert.code for alert in watch.alerts} == {
        "os_fail_closed",
        "audit_disabled",
        "boundary_downgrade",
    }


def test_boundary_watch_bounds_change_history_and_aggregated_alerts() -> None:
    enabled = project_security_shield(
        _tool_permissions(),
        sandbox_backend="disabled",
        audit_enabled=True,
    )
    disabled = project_security_shield(
        _tool_permissions(),
        sandbox_backend="disabled",
        audit_enabled=False,
    )

    watch = project_security_boundary_watch(
        (enabled, disabled, enabled, disabled, enabled),
        max_changes=2,
        max_alerts=1,
        observation_failures=3,
    )

    assert watch.observation_count == 5
    assert watch.total_change_count == 4
    assert len(watch.changes) == 2
    assert watch.dropped_change_count == 2
    assert len(watch.alerts) == 1
    assert watch.dropped_alert_count >= 1
    assert watch.alerts[0].code == "observation_failed"
    assert watch.alerts[0].occurrences == 3
    assert "PRIVATE-CANARY" not in repr(watch)
    assert MAX_SECURITY_BOUNDARY_CHANGES == 16
    assert MAX_SECURITY_BOUNDARY_ALERTS == 8


def test_approval_flow_joins_decision_and_final_binding_to_parent_tool() -> None:
    events = _approval_events(binding="changed")
    graph = ExecutionGraphProjector().project(reversed(events))

    flow = ApprovalFlowProjector().project(graph)
    trace = flow.traces[0]

    assert flow.schema_version == APPROVAL_FLOW_SCHEMA_VERSION
    assert trace.association == "linked"
    assert trace.tool_correlation_id == graph.nodes[0].correlation_id
    assert trace.tool_name == "write_file"
    assert trace.decision == "approved"
    assert trace.preview_binding == "changed"
    assert trace.preview_chars == 240
    assert trace.elapsed_ms == 12
    assert flow.changed_count == 1
    assert flow == ApprovalFlowProjector().project(
        ExecutionGraphProjector().project(events)
    )


def test_approval_flow_keeps_legacy_events_honest_about_unknown_binding() -> None:
    tool_start, approval_start, approval_finish, _ = _approval_events()
    legacy_finish = approval_finish.model_copy(update={"metadata": ()})
    graph = ExecutionGraphProjector().project(
        (tool_start, approval_start, legacy_finish)
    )

    trace = ApprovalFlowProjector().project(graph).traces[0]

    assert trace.decision == "approved"
    assert trace.preview_binding == "not_checked"


def test_approval_flow_accepts_the_runtime_contracts_bounded_tool_name() -> None:
    tool_name = "t" * 80
    graph = ExecutionGraphProjector().project(_approval_events(tool_name=tool_name))

    trace = ApprovalFlowProjector().project(graph).traces[0]

    assert trace.tool_name == tool_name


def test_projection_is_order_independent_and_does_not_retain_tool_names() -> None:
    permissions = _tool_permissions()
    forward = project_security_shield(
        permissions,
        sandbox_backend="disabled",
        audit_enabled=False,
    )
    reverse = project_security_shield(
        dict(reversed(tuple(permissions.items()))),
        sandbox_backend="disabled",
        audit_enabled=False,
    )

    assert forward == reverse
    assert not any(name in repr(forward) for name in permissions)


def test_unknown_tools_are_aggregated_without_exposing_their_names() -> None:
    shield = project_security_shield(
        {"PRIVATE-CANARY-TOOL": True},
        sandbox_backend="disabled",
        audit_enabled=False,
    )

    assert shield.tool_count == 1
    assert "PRIVATE-CANARY-TOOL" not in repr(shield)
    assert any(item.key == "other-approval" for item in shield.capabilities)


def test_observation_reports_unavailable_and_probe_failure_without_fallback() -> None:
    unavailable = SandboxCapabilities(
        backend="windows-sandbox",
        available=False,
        reason_code="executable_not_found",
        summary="PRIVATE-CANARY-SUMMARY",
    )
    probed = observe_security_shield(
        {},
        sandbox_backend="windows-sandbox",
        audit_enabled=False,
        sandbox_probe=lambda: unavailable,
    )
    failed = observe_security_shield(
        {},
        sandbox_backend="windows-sandbox",
        audit_enabled=False,
        sandbox_probe=lambda: (_ for _ in ()).throw(OSError("PRIVATE-CANARY")),
    )

    assert probed.os_sandbox.status == "unavailable"
    assert probed.os_sandbox.headline == "BACKEND NOT FOUND"
    assert "PRIVATE-CANARY-SUMMARY" not in repr(probed)
    assert failed.os_sandbox.headline == "PROBE FAILED · FAIL CLOSED"
    assert "PRIVATE-CANARY" not in repr(failed)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            AuditLogStatus(
                path=Path("audit.jsonl"),
                current_size_bytes=10,
                backup_size_bytes=0,
                max_bytes=1_000_000,
                current_records=None,
                backup_records=None,
                invalid_records=None,
                lock_available=False,
            ),
            "busy",
        ),
        (
            AuditLogStatus(
                path=Path("audit.jsonl"),
                current_size_bytes=10,
                backup_size_bytes=0,
                max_bytes=1_000_000,
                current_records=1,
                backup_records=0,
                invalid_records=1,
                lock_available=True,
            ),
            "degraded",
        ),
        (
            AuditLogStatus(
                path=Path("audit.jsonl"),
                current_size_bytes=10,
                backup_size_bytes=0,
                max_bytes=1_000_000,
                current_records=None,
                backup_records=0,
                invalid_records=0,
                lock_available=True,
            ),
            "unavailable",
        ),
    ],
)
def test_observation_derives_audit_health_without_retaining_paths(
    status: AuditLogStatus,
    expected: str,
) -> None:
    shield = observe_security_shield(
        _tool_permissions(),
        sandbox_backend="disabled",
        audit_enabled=True,
        audit_probe=lambda: status,
    )

    assert shield.audit_status == expected
    assert "audit.jsonl" not in repr(shield)


def test_observation_marks_audit_probe_failure_and_skips_disabled_probe() -> None:
    calls: list[str] = []
    unavailable = observe_security_shield(
        _tool_permissions(),
        sandbox_backend="disabled",
        audit_enabled=True,
        audit_probe=lambda: (_ for _ in ()).throw(AuditError("PRIVATE-CANARY")),
    )
    disabled = observe_security_shield(
        _tool_permissions(),
        sandbox_backend="disabled",
        audit_enabled=False,
        audit_probe=lambda: calls.append("called"),  # type: ignore[arg-type]
    )

    assert unavailable.audit_status == "unavailable"
    assert "PRIVATE-CANARY" not in repr(unavailable)
    assert disabled.audit_status == "disabled"
    assert calls == []


def test_projection_rejects_capabilities_from_another_sandbox_backend() -> None:
    mismatched = SandboxCapabilities(
        backend="other-backend",
        available=False,
        reason_code="unsupported_platform",
        summary="not Windows Sandbox",
    )

    shield = project_security_shield(
        {},
        sandbox_backend="windows-sandbox",
        audit_enabled=False,
        sandbox_capabilities=mismatched,
    )

    assert shield.os_sandbox.status == "unavailable"
    assert shield.os_sandbox.headline == "BACKEND MISMATCH · FAIL CLOSED"


def test_disabled_backend_does_not_run_probe() -> None:
    calls: list[str] = []

    shield = observe_security_shield(
        {},
        sandbox_backend="disabled",
        audit_enabled=False,
        sandbox_probe=lambda: calls.append("probe"),  # type: ignore[arg-type]
    )

    assert calls == []
    assert shield.os_sandbox.status == "disabled"


def test_security_models_reject_invalid_or_contradictory_metadata() -> None:
    with pytest.raises(ValueError, match="blocked capabilities"):
        SecurityCapability(
            "host-shell",
            "HOST SHELL",
            "forbidden",
            "application",
            1,
            "must remain absent",
        )
    with pytest.raises(ValueError, match="unknown sandbox backend"):
        project_security_shield(
            {},
            sandbox_backend="host-process",
            audit_enabled=False,
        )
    with pytest.raises(ValueError, match="permission map"):
        project_security_shield(
            {1: False},  # type: ignore[dict-item]
            sandbox_backend="disabled",
            audit_enabled=False,
        )
    with pytest.raises(ValueError, match="audit configuration"):
        project_security_shield(
            {},
            sandbox_backend="disabled",
            audit_enabled=False,
            audit_status="recording",
        )
    with pytest.raises(ValueError, match="requires an observation"):
        project_security_boundary_watch(())
