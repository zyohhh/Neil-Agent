"""Tests for the deterministic, metadata-only Security Shield projection."""

from __future__ import annotations

import pytest

from neil_agent.sandbox import SandboxCapabilities
from neil_agent.security import (
    SECURITY_SHIELD_SCHEMA_VERSION,
    SecurityCapability,
    observe_security_shield,
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
    }


def test_projection_builds_four_bands_and_keeps_enforcement_layers_distinct() -> None:
    shield = project_security_shield(
        _tool_permissions(),
        sandbox_backend="disabled",
        audit_enabled=True,
    )

    assert shield.schema_version == SECURITY_SHIELD_SCHEMA_VERSION
    assert shield.tool_count == 12
    assert shield.direct_tool_count == 7
    assert shield.approval_tool_count == 5
    assert shield.capability_count("direct") == 3
    assert shield.capability_count("approval") == 3
    assert shield.capability_count("forbidden") == 1
    assert shield.capability_count("unavailable") == 1
    assert shield.application.layer == "application"
    assert shield.application.status == "enforced"
    assert shield.os_sandbox.layer == "os"
    assert shield.os_sandbox.status == "disabled"
    assert "not a sandbox" in " ".join(shield.os_sandbox.details)


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
