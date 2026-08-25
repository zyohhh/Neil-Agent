"""Shared Security Shield basic projections for cockpit and Web DTOs."""

from __future__ import annotations

from dataclasses import dataclass

from .security import (
    SecurityBoundarySignal,
    SecurityBoundaryWatch,
    SecurityShield,
    project_security_boundary_watch,
)


@dataclass(frozen=True, slots=True)
class SecurityCapabilityLegend:
    """Compact capability-band counts shown in cockpit and Web security panels."""

    direct: int
    approval: int
    forbidden: int
    unavailable: int


@dataclass(frozen=True, slots=True)
class SecurityShieldBasicProjection:
    """Deterministic Security Shield facts aligned with cockpit SECURITY SHIELD · BASIC."""

    shield: SecurityShield
    boundary_watch: SecurityBoundaryWatch
    capability_legend: SecurityCapabilityLegend


def basic_boundary_label(signal: SecurityBoundarySignal) -> str:
    """Return one fixed, value-free label for a boundary signal."""

    labels = {
        "path": {
            "enforced": "PATH OS",
            "application_only": "PATH APP",
            "absent": "PATH NONE",
        },
        "network": {
            "enforced": "NETWORK DENY",
            "absent": "NETWORK ABSENT",
        },
        "command": {
            "restricted": "COMMAND FIXED",
            "absent": "COMMAND NONE",
        },
        "audit": {
            "recording": "AUDIT RECORDING",
            "busy": "AUDIT BUSY",
            "disabled": "AUDIT DISABLED",
            "degraded": "AUDIT DEGRADED",
            "unavailable": "AUDIT UNAVAILABLE",
        },
    }
    return labels.get(signal.key, {}).get(signal.state, "UNKNOWN")


def project_security_shield_basic(shield: SecurityShield) -> SecurityShieldBasicProjection:
    """Project one Security Shield snapshot plus its single-observation boundary watch."""

    boundary_watch = project_security_boundary_watch((shield,))
    return SecurityShieldBasicProjection(
        shield=shield,
        boundary_watch=boundary_watch,
        capability_legend=SecurityCapabilityLegend(
            direct=shield.capability_count("direct"),
            approval=shield.capability_count("approval"),
            forbidden=shield.capability_count("forbidden"),
            unavailable=shield.capability_count("unavailable"),
        ),
    )
