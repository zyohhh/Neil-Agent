"""Deterministic, metadata-only projections for the Security Shield."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from .errors import SandboxError
from .sandbox import SandboxCapabilities, WindowsSandboxBackend

SECURITY_SHIELD_SCHEMA_VERSION = 1
MAX_SECURITY_LABEL_CHARS = 48
MAX_SECURITY_SUMMARY_CHARS = 120

CapabilityState = Literal["direct", "approval", "forbidden", "unavailable"]
SecurityLayer = Literal["application", "os"]
BoundaryStatus = Literal["enforced", "ready", "disabled", "incomplete", "unavailable"]

_CAPABILITY_STATES = frozenset({"direct", "approval", "forbidden", "unavailable"})
_LAYERS = frozenset({"application", "os"})
_BOUNDARY_STATUSES = frozenset(
    {"enforced", "ready", "disabled", "incomplete", "unavailable"}
)


@dataclass(frozen=True, slots=True)
class _CapabilityGroup:
    key: str
    label: str
    layer: SecurityLayer
    names: tuple[str, ...]
    summary: str


_CAPABILITY_GROUPS = (
    _CapabilityGroup(
        "workspace-read",
        "WORKSPACE READ",
        "application",
        ("list_directory", "read_file", "search_text"),
        "bounded paths · sensitive files blocked",
    ),
    _CapabilityGroup(
        "task-control",
        "TASK CONTROL",
        "application",
        ("set_task_plan", "update_task_step"),
        "in-memory plan state only",
    ),
    _CapabilityGroup(
        "git-inspect",
        "GIT INSPECT",
        "application",
        ("git_status", "git_diff"),
        "read-only repository metadata",
    ),
    _CapabilityGroup(
        "workspace-write",
        "WORKSPACE WRITE",
        "application",
        ("write_file", "replace_text"),
        "preview-bound workspace mutation",
    ),
    _CapabilityGroup(
        "quality-run",
        "QUALITY RUN",
        "application",
        ("run_quality_check",),
        "fixed commands · bounded output",
    ),
    _CapabilityGroup(
        "git-mutate",
        "GIT MUTATE",
        "application",
        ("git_stage", "git_commit"),
        "explicit paths · local commit only",
    ),
    _CapabilityGroup(
        "os-command",
        "OS COMMAND",
        "os",
        ("sandbox_run_command",),
        "no Agent execution tool exposed",
    ),
)
_SANDBOX_REASON_LABELS = {
    "unsupported_platform": "UNSUPPORTED PLATFORM",
    "executable_not_found": "BACKEND NOT FOUND",
    "cli_executable_required": "CLI REQUIRED",
    "certification_required": "CERTIFICATION REQUIRED",
    "execution_channel_unavailable": "EXECUTION CHANNEL UNAVAILABLE",
    "capability_incomplete": "CAPABILITY GATES INCOMPLETE",
    "ready": "ALL GATES READY",
}


@dataclass(frozen=True, slots=True)
class SecurityCapability:
    """One bounded capability band without arguments, values, or content."""

    key: str
    label: str
    state: CapabilityState
    layer: SecurityLayer
    tool_count: int
    summary: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.key, str)
            or not self.key
            or len(self.key) > MAX_SECURITY_LABEL_CHARS
        ):
            raise ValueError("security capability key is invalid")
        if (
            not isinstance(self.label, str)
            or not self.label
            or len(self.label) > MAX_SECURITY_LABEL_CHARS
        ):
            raise ValueError("security capability label is invalid")
        if self.state not in _CAPABILITY_STATES:
            raise ValueError("security capability state is invalid")
        if self.layer not in _LAYERS:
            raise ValueError("security capability layer is invalid")
        if (
            isinstance(self.tool_count, bool)
            or not isinstance(self.tool_count, int)
            or self.tool_count < 0
        ):
            raise ValueError("security capability tool count cannot be negative")
        if (
            not isinstance(self.summary, str)
            or not self.summary
            or len(self.summary) > MAX_SECURITY_SUMMARY_CHARS
        ):
            raise ValueError("security capability summary is invalid")
        if self.state in {"forbidden", "unavailable"} and self.tool_count:
            raise ValueError("blocked capabilities cannot expose registered tools")


@dataclass(frozen=True, slots=True)
class SecurityBoundary:
    """Status for one explicitly named enforcement layer."""

    layer: SecurityLayer
    status: BoundaryStatus
    headline: str
    details: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.layer not in _LAYERS:
            raise ValueError("security boundary layer is invalid")
        if self.status not in _BOUNDARY_STATUSES:
            raise ValueError("security boundary status is invalid")
        if (
            not isinstance(self.headline, str)
            or not self.headline
            or len(self.headline) > MAX_SECURITY_SUMMARY_CHARS
        ):
            raise ValueError("security boundary headline is invalid")
        if (
            not isinstance(self.details, tuple)
            or not self.details
            or any(
                not isinstance(detail, str)
                or not detail
                or len(detail) > MAX_SECURITY_SUMMARY_CHARS
                for detail in self.details
            )
        ):
            raise ValueError("security boundary details are invalid")


@dataclass(frozen=True, slots=True)
class SecurityShield:
    """Versioned Security Shield projection consumed by Rich and Textual."""

    capabilities: tuple[SecurityCapability, ...]
    application: SecurityBoundary
    os_sandbox: SecurityBoundary
    tool_count: int
    direct_tool_count: int
    approval_tool_count: int
    audit_enabled: bool
    schema_version: int = SECURITY_SHIELD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or self.schema_version != SECURITY_SHIELD_SCHEMA_VERSION
        ):
            raise ValueError("unsupported Security Shield schema version")
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise ValueError("security shield requires capability bands")
        if any(
            not isinstance(capability, SecurityCapability)
            for capability in self.capabilities
        ):
            raise ValueError("security shield capabilities are invalid")
        if len({item.key for item in self.capabilities}) != len(self.capabilities):
            raise ValueError("security capability keys must be unique")
        for value in (
            self.tool_count,
            self.direct_tool_count,
            self.approval_tool_count,
        ):
            if isinstance(value, bool) or value < 0:
                raise ValueError("security tool counts cannot be negative")
        if self.direct_tool_count + self.approval_tool_count != self.tool_count:
            raise ValueError("security tool counts must be exhaustive")
        if self.application.layer != "application" or self.os_sandbox.layer != "os":
            raise ValueError(
                "security boundaries must keep distinct enforcement layers"
            )
        if type(self.audit_enabled) is not bool:
            raise ValueError("security audit state must be boolean")

    def capability_count(self, state: CapabilityState) -> int:
        """Count bands in one state for compact summaries and legends."""

        return sum(capability.state == state for capability in self.capabilities)


def observe_security_shield(
    tool_permissions: Mapping[str, bool],
    *,
    sandbox_backend: str,
    audit_enabled: bool,
    sandbox_probe: Callable[[], SandboxCapabilities] | None = None,
) -> SecurityShield:
    """Capture optional OS availability, then run the deterministic projection."""

    capabilities: SandboxCapabilities | None = None
    probe_failed = False
    if sandbox_backend == "windows-sandbox":
        try:
            probe = sandbox_probe or WindowsSandboxBackend().probe
            capabilities = probe()
        except (OSError, RuntimeError, SandboxError, ValueError):
            probe_failed = True
    return project_security_shield(
        tool_permissions,
        sandbox_backend=sandbox_backend,
        audit_enabled=audit_enabled,
        sandbox_capabilities=capabilities,
        sandbox_probe_failed=probe_failed,
    )


def project_security_shield(
    tool_permissions: Mapping[str, bool],
    *,
    sandbox_backend: str,
    audit_enabled: bool,
    sandbox_capabilities: SandboxCapabilities | None = None,
    sandbox_probe_failed: bool = False,
) -> SecurityShield:
    """Project stable capability bands from already-observed runtime facts."""

    if sandbox_backend not in {"disabled", "windows-sandbox"}:
        raise ValueError("unknown sandbox backend")
    if type(audit_enabled) is not bool or type(sandbox_probe_failed) is not bool:
        raise ValueError("security observation flags must be boolean")
    if any(
        not isinstance(name, str) or not name or not isinstance(requires_approval, bool)
        for name, requires_approval in tool_permissions.items()
    ):
        raise ValueError("tool permission map is invalid")
    permissions = dict(sorted(tool_permissions.items()))

    bands: list[SecurityCapability] = []
    grouped_names: set[str] = set()
    for group in _CAPABILITY_GROUPS:
        grouped_names.update(group.names)
        present = tuple(name for name in group.names if name in permissions)
        direct = sum(not permissions[name] for name in present)
        approval = len(present) - direct
        if direct and approval:
            bands.extend(
                (
                    SecurityCapability(
                        f"{group.key}-direct",
                        f"{group.label} · READ",
                        "direct",
                        group.layer,
                        direct,
                        group.summary,
                    ),
                    SecurityCapability(
                        f"{group.key}-approval",
                        f"{group.label} · CHANGE",
                        "approval",
                        group.layer,
                        approval,
                        group.summary,
                    ),
                )
            )
            continue
        state: CapabilityState
        if approval:
            state = "approval"
        elif direct:
            state = "direct"
        else:
            state = "unavailable"
        bands.append(
            SecurityCapability(
                group.key,
                group.label,
                state,
                group.layer,
                len(present),
                group.summary,
            )
        )

    unknown = tuple(name for name in permissions if name not in grouped_names)
    unknown_states: tuple[tuple[CapabilityState, bool], ...] = (
        ("direct", False),
        ("approval", True),
    )
    for state, requires_approval in unknown_states:
        count = sum(permissions[name] is requires_approval for name in unknown)
        if count:
            bands.append(
                SecurityCapability(
                    f"other-{state}",
                    "OTHER ALLOWLISTED",
                    state,
                    "application",
                    count,
                    "registered capability outside the fixed display groups",
                )
            )
    bands.append(
        SecurityCapability(
            "host-shell",
            "ARBITRARY HOST SHELL",
            "forbidden",
            "application",
            0,
            "permanently absent from the application tool surface",
        )
    )

    direct_count = sum(not requires for requires in permissions.values())
    approval_count = len(permissions) - direct_count
    application = SecurityBoundary(
        "application",
        "enforced",
        "ALLOWLIST ENFORCED",
        (
            f"{len(permissions)} tools · {direct_count} direct · {approval_count} approval",
            "workspace + sensitive paths constrained",
            "host shell + local-tool network absent",
            f"metadata audit {'enabled' if audit_enabled else 'disabled'}",
        ),
    )
    os_boundary = _project_os_boundary(
        sandbox_backend,
        sandbox_capabilities,
        probe_failed=sandbox_probe_failed,
    )
    return SecurityShield(
        capabilities=tuple(bands),
        application=application,
        os_sandbox=os_boundary,
        tool_count=len(permissions),
        direct_tool_count=direct_count,
        approval_tool_count=approval_count,
        audit_enabled=audit_enabled,
    )


def _project_os_boundary(
    backend: str,
    capabilities: SandboxCapabilities | None,
    *,
    probe_failed: bool,
) -> SecurityBoundary:
    if backend == "disabled":
        return SecurityBoundary(
            "os",
            "disabled",
            "DISABLED BY CONFIG",
            (
                "OS-isolated command execution is inactive",
                "application allowlisting is active but is not a sandbox",
            ),
        )
    if probe_failed or capabilities is None:
        return SecurityBoundary(
            "os",
            "unavailable",
            "PROBE FAILED · FAIL CLOSED",
            (
                "no command was launched during the read-only probe",
                "application allowlisting remains a separate layer",
            ),
        )
    if capabilities.backend != backend:
        return SecurityBoundary(
            "os",
            "unavailable",
            "BACKEND MISMATCH · FAIL CLOSED",
            (
                "observed capability evidence belongs to another backend",
                "application allowlisting remains a separate layer",
            ),
        )
    reason = _SANDBOX_REASON_LABELS.get(capabilities.reason_code, "UNKNOWN STATUS")
    if capabilities.ready:
        return SecurityBoundary(
            "os",
            "ready",
            reason,
            (
                "certification and mandatory capability gates are bound",
                "no Agent OS-command tool is exposed in this phase",
            ),
        )
    status: BoundaryStatus = "incomplete" if capabilities.available else "unavailable"
    return SecurityBoundary(
        "os",
        status,
        reason,
        (
            "backend is not approved for Agent command execution",
            "failure does not fall back to a host process",
        ),
    )
