"""Tests for shared Security Shield basic projections."""

from __future__ import annotations

from neil_agent.security import project_security_shield
from neil_agent.security_projection import (
    basic_boundary_label,
    project_security_shield_basic,
)
from neil_agent.web.dto import SecurityDto


def _tool_permissions() -> dict[str, bool]:
    return {
        "read_file": False,
        "write_file": True,
        "run_quality_check": True,
        "run_command": True,
    }


def test_basic_boundary_label_matches_cockpit_tokens() -> None:
    shield = project_security_shield(
        _tool_permissions(),
        sandbox_backend="disabled",
        audit_enabled=False,
    )
    watch = project_security_shield_basic(shield).boundary_watch

    labels = [basic_boundary_label(signal) for signal in watch.signals]
    assert labels == [
        "PATH APP",
        "NETWORK ABSENT",
        "COMMAND FIXED",
        "AUDIT DISABLED",
    ]


def test_security_dto_from_shield_matches_basic_projection() -> None:
    shield = project_security_shield(
        _tool_permissions(),
        sandbox_backend="disabled",
        audit_enabled=True,
        audit_status="recording",
    )
    projection = project_security_shield_basic(shield)
    dto = SecurityDto.from_security_shield(shield, sandbox_backend="disabled")

    assert dto.shield_schema_version == 2
    assert dto.tool_count == shield.tool_count
    assert dto.direct_tool_count == shield.direct_tool_count
    assert dto.approval_tool_count == shield.approval_tool_count
    assert dto.application.headline == shield.application.headline
    assert dto.os_sandbox.headline == shield.os_sandbox.headline
    assert dto.capability_legend.direct == projection.capability_legend.direct
    assert dto.boundary_watch.changes_stable is True
    assert len(dto.boundary_watch.signals) == 4
    assert dto.boundary_watch.signals[3].label == "AUDIT RECORDING"
