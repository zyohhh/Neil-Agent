"""Deterministic, metadata-only projections for the Security Shield."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, cast

from .audit import AuditLogStatus
from .errors import AuditError, SandboxError
from .events import (
    MAX_RUNTIME_METADATA_TEXT_CHARS,
    ApprovalDecision,
    PreviewBindingState,
    RuntimeMetadataItem,
)
from .projections import ExecutionGraph, ExecutionNode
from .sandbox import SandboxCapabilities, WindowsSandboxBackend

SECURITY_SHIELD_SCHEMA_VERSION = 2
APPROVAL_FLOW_SCHEMA_VERSION = 1
SECURITY_BOUNDARY_WATCH_SCHEMA_VERSION = 1
MAX_SECURITY_LABEL_CHARS = 48
MAX_SECURITY_SUMMARY_CHARS = 120
MAX_SECURITY_BOUNDARY_CHANGES = 16
MAX_SECURITY_BOUNDARY_ALERTS = 8

CapabilityState = Literal["direct", "approval", "forbidden", "unavailable"]
SecurityLayer = Literal["application", "os"]
BoundaryStatus = Literal["enforced", "ready", "disabled", "incomplete", "unavailable"]
ApprovalAssociation = Literal["linked", "unresolved"]
AuditBoundaryStatus = Literal[
    "recording",
    "busy",
    "disabled",
    "degraded",
    "unavailable",
]
SecurityBoundaryKey = Literal["path", "network", "command", "audit"]
SecurityBoundaryState = Literal[
    "enforced",
    "application_only",
    "restricted",
    "absent",
    "recording",
    "busy",
    "disabled",
    "degraded",
    "unavailable",
]
SecurityBoundaryQualifier = Literal[
    "os_ready",
    "os_disabled",
    "os_fail_closed",
    "application",
]
SecurityAlertSeverity = Literal["information", "warning", "critical"]
SecurityAlertScope = Literal["path", "network", "command", "audit", "observer"]
SecurityAlertCode = Literal[
    "os_disabled",
    "os_fail_closed",
    "audit_busy",
    "audit_disabled",
    "audit_degraded",
    "audit_unavailable",
    "observation_failed",
    "boundary_downgrade",
]

_CAPABILITY_STATES = frozenset({"direct", "approval", "forbidden", "unavailable"})
_LAYERS = frozenset({"application", "os"})
_BOUNDARY_STATUSES = frozenset(
    {"enforced", "ready", "disabled", "incomplete", "unavailable"}
)
_AUDIT_BOUNDARY_STATUSES = frozenset(
    {"recording", "busy", "disabled", "degraded", "unavailable"}
)
_SECURITY_BOUNDARY_ORDER: tuple[SecurityBoundaryKey, ...] = (
    "path",
    "network",
    "command",
    "audit",
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
        "readonly-subtask",
        "READONLY SUBTASK",
        "application",
        ("run_readonly_subtask",),
        "one-shot read-only child runtime",
    ),
    _CapabilityGroup(
        "skill-load",
        "SKILL LOAD",
        "application",
        ("load_skill",),
        "bounded SKILL.md · untrusted request context",
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
        ("run_command",),
        "certified sandbox only · explicit executable + argv",
    ),
)
_SANDBOX_REASON_LABELS = {
    "unsupported_platform": "UNSUPPORTED PLATFORM",
    "executable_not_found": "BACKEND NOT FOUND",
    "cli_executable_required": "CLI REQUIRED",
    "certification_required": "CERTIFICATION REQUIRED",
    "certification_invalid": "CERTIFICATION INVALID",
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
    audit_status: AuditBoundaryStatus
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
        if self.audit_status not in _AUDIT_BOUNDARY_STATUSES:
            raise ValueError("security audit boundary status is invalid")
        if self.audit_enabled == (self.audit_status == "disabled"):
            raise ValueError("security audit configuration and status contradict")

    def capability_count(self, state: CapabilityState) -> int:
        """Count bands in one state for compact summaries and legends."""

        return sum(capability.state == state for capability in self.capabilities)


@dataclass(frozen=True, slots=True)
class SecurityBoundarySignal:
    """One fixed, value-free posture for a monitored security boundary."""

    key: SecurityBoundaryKey
    state: SecurityBoundaryState
    layer: SecurityLayer
    qualifier: SecurityBoundaryQualifier

    def __post_init__(self) -> None:
        if self.key not in _SECURITY_BOUNDARY_ORDER:
            raise ValueError("security boundary signal key is invalid")
        allowed = {
            "path": {
                ("enforced", "os", "os_ready"),
                ("application_only", "application", "os_ready"),
                ("application_only", "application", "os_disabled"),
                ("application_only", "application", "os_fail_closed"),
                ("absent", "application", "application"),
            },
            "network": {
                ("enforced", "os", "os_ready"),
                ("absent", "application", "os_ready"),
                ("absent", "application", "os_disabled"),
                ("absent", "application", "os_fail_closed"),
            },
            "command": {
                ("restricted", "application", "application"),
                ("restricted", "os", "os_ready"),
                ("absent", "application", "application"),
            },
            "audit": {
                ("recording", "application", "application"),
                ("busy", "application", "application"),
                ("disabled", "application", "application"),
                ("degraded", "application", "application"),
                ("unavailable", "application", "application"),
            },
        }[self.key]
        if (self.state, self.layer, self.qualifier) not in allowed:
            raise ValueError("security boundary signal combination is invalid")


@dataclass(frozen=True, slots=True)
class SecurityBoundaryChange:
    """One deterministic boundary transition between adjacent observations."""

    observation_index: int
    before: SecurityBoundarySignal
    after: SecurityBoundarySignal
    severity: SecurityAlertSeverity

    def __post_init__(self) -> None:
        if (
            isinstance(self.observation_index, bool)
            or not isinstance(self.observation_index, int)
            or self.observation_index < 2
        ):
            raise ValueError("security boundary change index is invalid")
        if not isinstance(self.before, SecurityBoundarySignal) or not isinstance(
            self.after, SecurityBoundarySignal
        ):
            raise ValueError("security boundary change signals are invalid")
        if self.before.key != self.after.key or self.before == self.after:
            raise ValueError("security boundary change must alter one boundary")
        if self.severity not in {"information", "warning", "critical"}:
            raise ValueError("security boundary change severity is invalid")


@dataclass(frozen=True, slots=True)
class SecurityBoundaryAlert:
    """One aggregate, coded alert without paths, commands, or audit content."""

    scope: SecurityAlertScope
    code: SecurityAlertCode
    severity: SecurityAlertSeverity
    observation_index: int
    occurrences: int = 1

    def __post_init__(self) -> None:
        if self.scope not in {*_SECURITY_BOUNDARY_ORDER, "observer"}:
            raise ValueError("security boundary alert scope is invalid")
        if self.code not in {
            "os_disabled",
            "os_fail_closed",
            "audit_busy",
            "audit_disabled",
            "audit_degraded",
            "audit_unavailable",
            "observation_failed",
            "boundary_downgrade",
        }:
            raise ValueError("security boundary alert code is invalid")
        if self.severity not in {"information", "warning", "critical"}:
            raise ValueError("security boundary alert severity is invalid")
        for value in (self.observation_index, self.occurrences):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("security boundary alert count is invalid")


@dataclass(frozen=True, slots=True)
class SecurityBoundaryWatch:
    """Versioned, bounded state changes for four metadata-only boundaries."""

    signals: tuple[SecurityBoundarySignal, ...]
    changes: tuple[SecurityBoundaryChange, ...]
    alerts: tuple[SecurityBoundaryAlert, ...]
    observation_count: int
    total_change_count: int
    dropped_change_count: int
    dropped_alert_count: int
    observation_failures: int = 0
    schema_version: int = SECURITY_BOUNDARY_WATCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SECURITY_BOUNDARY_WATCH_SCHEMA_VERSION:
            raise ValueError("unsupported security boundary watch schema version")
        if any(
            not isinstance(signal, SecurityBoundarySignal) for signal in self.signals
        ):
            raise ValueError("security boundary watch signals are invalid")
        if tuple(signal.key for signal in self.signals) != _SECURITY_BOUNDARY_ORDER:
            raise ValueError("security boundary watch signals are incomplete")
        if len(self.changes) > MAX_SECURITY_BOUNDARY_CHANGES or any(
            not isinstance(change, SecurityBoundaryChange) for change in self.changes
        ):
            raise ValueError("security boundary watch changes are invalid")
        if len(self.alerts) > MAX_SECURITY_BOUNDARY_ALERTS or any(
            not isinstance(alert, SecurityBoundaryAlert) for alert in self.alerts
        ):
            raise ValueError("security boundary watch alerts are invalid")
        for value in (
            self.observation_count,
            self.total_change_count,
            self.dropped_change_count,
            self.dropped_alert_count,
            self.observation_failures,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("security boundary watch count is invalid")
        if self.observation_count < 1:
            raise ValueError("security boundary watch requires an observation")
        if self.total_change_count != len(self.changes) + self.dropped_change_count:
            raise ValueError("security boundary watch change counts contradict")
        if any(
            change.observation_index > self.observation_count for change in self.changes
        ) or any(
            alert.observation_index > self.observation_count for alert in self.alerts
        ):
            raise ValueError("security boundary watch observation index contradicts")

    def signal(self, key: SecurityBoundaryKey) -> SecurityBoundarySignal:
        """Return one of the four fixed current boundary signals."""

        return self.signals[_SECURITY_BOUNDARY_ORDER.index(key)]

    @property
    def warning_count(self) -> int:
        return sum(alert.severity in {"warning", "critical"} for alert in self.alerts)


@dataclass(frozen=True, slots=True)
class ApprovalTrace:
    """One approval node joined to its parent tool without preview content."""

    correlation_id: str
    tool_correlation_id: str | None
    tool_name: str
    association: ApprovalAssociation
    decision: ApprovalDecision
    preview_binding: PreviewBindingState
    preview_chars: int
    elapsed_ms: int | None

    def __post_init__(self) -> None:
        if not self.correlation_id.startswith("approval-"):
            raise ValueError("approval trace correlation ID is invalid")
        if self.association == "linked":
            if (
                self.tool_correlation_id is None
                or not self.tool_correlation_id.startswith("tool-")
            ):
                raise ValueError("linked approval trace requires a tool node")
        elif self.tool_correlation_id is not None:
            raise ValueError("unresolved approval trace cannot claim a tool node")
        if not self.tool_name or len(self.tool_name) > MAX_RUNTIME_METADATA_TEXT_CHARS:
            raise ValueError("approval trace tool name is invalid")
        if self.decision not in {
            "pending",
            "approved",
            "rejected",
            "unavailable",
            "error",
        }:
            raise ValueError("approval trace decision is invalid")
        if self.preview_binding not in {
            "pending",
            "valid",
            "changed",
            "unavailable",
            "not_checked",
        }:
            raise ValueError("approval trace preview binding is invalid")
        if (
            isinstance(self.preview_chars, bool)
            or not isinstance(self.preview_chars, int)
            or self.preview_chars < 0
        ):
            raise ValueError("approval trace preview size is invalid")
        if self.elapsed_ms is not None and (
            isinstance(self.elapsed_ms, bool)
            or not isinstance(self.elapsed_ms, int)
            or self.elapsed_ms < 0
        ):
            raise ValueError("approval trace elapsed time is invalid")


@dataclass(frozen=True, slots=True)
class ApprovalFlow:
    """Versioned, deterministic approvals projected from one execution graph."""

    traces: tuple[ApprovalTrace, ...]
    schema_version: int = APPROVAL_FLOW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or self.schema_version != APPROVAL_FLOW_SCHEMA_VERSION
        ):
            raise ValueError("unsupported approval flow schema version")
        if not isinstance(self.traces, tuple) or any(
            not isinstance(trace, ApprovalTrace) for trace in self.traces
        ):
            raise ValueError("approval flow traces must be immutable")
        if len({trace.correlation_id for trace in self.traces}) != len(self.traces):
            raise ValueError("approval flow traces must be unique")

    @property
    def pending_count(self) -> int:
        return sum(trace.decision == "pending" for trace in self.traces)

    @property
    def changed_count(self) -> int:
        return sum(trace.preview_binding == "changed" for trace in self.traces)

    def trace(self, correlation_id: str) -> ApprovalTrace | None:
        return next(
            (trace for trace in self.traces if trace.correlation_id == correlation_id),
            None,
        )


class ApprovalFlowProjector:
    """Join approval nodes with parent tool outcomes from a stable DAG."""

    def project(self, graph: ExecutionGraph) -> ApprovalFlow:
        nodes = {node.correlation_id: node for node in graph.nodes}
        traces = tuple(
            self._trace(node, nodes) for node in graph.nodes if node.stage == "approval"
        )
        return ApprovalFlow(traces=traces)

    @staticmethod
    def _trace(
        approval: ExecutionNode,
        nodes: Mapping[str, ExecutionNode],
    ) -> ApprovalTrace:
        parent = (
            nodes.get(approval.parent_correlation_id)
            if approval.parent_correlation_id is not None
            else None
        )
        linked_tool = (
            parent if parent is not None and parent.stage == "tool_call" else None
        )
        decision = _approval_decision(approval)
        binding = _preview_binding(approval)
        if linked_tool is not None:
            tool_decision = _text_metadata(
                linked_tool.finish_metadata,
                "approval_decision",
            )
            tool_binding = _text_metadata(
                linked_tool.finish_metadata,
                "preview_binding",
            )
            if tool_decision == "approved" and tool_binding in {
                "valid",
                "changed",
                "unavailable",
            }:
                decision = "approved"
                binding = cast(PreviewBindingState, tool_binding)
        tool_name = _text_metadata(approval.start_metadata, "tool_name")
        if tool_name is None and linked_tool is not None:
            tool_name = _text_metadata(linked_tool.start_metadata, "tool_name")
        preview_chars = _integer_metadata(approval.start_metadata, "preview_chars")
        elapsed_ms = _integer_metadata(approval.finish_metadata, "elapsed_ms")
        return ApprovalTrace(
            correlation_id=approval.correlation_id,
            tool_correlation_id=(
                None if linked_tool is None else linked_tool.correlation_id
            ),
            tool_name=tool_name or "UNKNOWN TOOL",
            association="unresolved" if linked_tool is None else "linked",
            decision=decision,
            preview_binding=binding,
            preview_chars=preview_chars or 0,
            elapsed_ms=elapsed_ms,
        )


def _approval_decision(node: ExecutionNode) -> ApprovalDecision:
    value = _text_metadata(node.finish_metadata, "approval_decision")
    if value is None and node.status == "waiting":
        value = _text_metadata(node.start_metadata, "approval_decision")
    if value in {"pending", "approved", "rejected", "unavailable", "error"}:
        return cast(ApprovalDecision, value)
    return {
        "waiting": "pending",
        "succeeded": "approved",
        "skipped": "rejected",
        "failed": "error",
    }.get(node.status, "error")  # type: ignore[return-value]


def _preview_binding(node: ExecutionNode) -> PreviewBindingState:
    value = _text_metadata(node.finish_metadata, "preview_binding")
    if value is None and node.status == "waiting":
        value = _text_metadata(node.start_metadata, "preview_binding")
    if value in {"pending", "valid", "changed", "unavailable", "not_checked"}:
        return cast(PreviewBindingState, value)
    return "pending" if node.status == "waiting" else "not_checked"


def _text_metadata(
    metadata: tuple[RuntimeMetadataItem, ...],
    name: str,
) -> str | None:
    value = next((item.value for item in metadata if item.name == name), None)
    return value if isinstance(value, str) else None


def _integer_metadata(
    metadata: tuple[RuntimeMetadataItem, ...],
    name: str,
) -> int | None:
    value = next((item.value for item in metadata if item.name == name), None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def project_security_boundary_watch(
    observations: Iterable[SecurityShield],
    *,
    max_changes: int = MAX_SECURITY_BOUNDARY_CHANGES,
    max_alerts: int = MAX_SECURITY_BOUNDARY_ALERTS,
    observation_failures: int = 0,
) -> SecurityBoundaryWatch:
    """Project four current boundaries plus bounded adjacent state changes."""

    if (
        isinstance(max_changes, bool)
        or not isinstance(max_changes, int)
        or not 1 <= max_changes <= MAX_SECURITY_BOUNDARY_CHANGES
    ):
        raise ValueError("security boundary change limit is invalid")
    if (
        isinstance(max_alerts, bool)
        or not isinstance(max_alerts, int)
        or not 1 <= max_alerts <= MAX_SECURITY_BOUNDARY_ALERTS
    ):
        raise ValueError("security boundary alert limit is invalid")
    if (
        isinstance(observation_failures, bool)
        or not isinstance(observation_failures, int)
        or observation_failures < 0
    ):
        raise ValueError("security observation failure count is invalid")

    retained_changes: deque[SecurityBoundaryChange] = deque(maxlen=max_changes)
    previous: tuple[SecurityBoundarySignal, ...] | None = None
    current: tuple[SecurityBoundarySignal, ...] | None = None
    observation_count = 0
    total_change_count = 0
    for observation_count, shield in enumerate(observations, start=1):
        if not isinstance(shield, SecurityShield):
            raise ValueError("security boundary observations are invalid")
        current = _security_boundary_signals(shield)
        if previous is not None:
            for before, after in zip(previous, current, strict=True):
                if before == after:
                    continue
                total_change_count += 1
                retained_changes.append(
                    SecurityBoundaryChange(
                        observation_index=observation_count,
                        before=before,
                        after=after,
                        severity=(
                            "warning"
                            if _security_boundary_risk(after)
                            > _security_boundary_risk(before)
                            else "information"
                        ),
                    )
                )
        previous = current
    if current is None:
        raise ValueError("security boundary watch requires an observation")

    changes = tuple(retained_changes)
    alerts = _security_boundary_alerts(
        current,
        changes,
        observation_count=observation_count,
        observation_failures=observation_failures,
    )
    dropped_alert_count = max(len(alerts) - max_alerts, 0)
    retained_alerts = alerts[-max_alerts:]
    return SecurityBoundaryWatch(
        signals=current,
        changes=changes,
        alerts=retained_alerts,
        observation_count=observation_count,
        total_change_count=total_change_count,
        dropped_change_count=total_change_count - len(changes),
        dropped_alert_count=dropped_alert_count,
        observation_failures=observation_failures,
    )


def _security_boundary_signals(
    security: SecurityShield,
) -> tuple[SecurityBoundarySignal, ...]:
    os_qualifier: SecurityBoundaryQualifier = (
        "os_ready"
        if security.os_sandbox.status == "ready"
        else "os_disabled"
        if security.os_sandbox.status == "disabled"
        else "os_fail_closed"
    )
    path_tools = any(
        capability.tool_count
        and capability.key.startswith(("workspace-read", "workspace-write"))
        for capability in security.capabilities
    )
    command_tools = any(
        capability.tool_count
        and capability.key.startswith(("quality-run", "git-mutate"))
        for capability in security.capabilities
    )
    os_command_exposed = any(
        capability.tool_count
        and capability.key.startswith("os-command")
        and capability.state in {"direct", "approval"}
        for capability in security.capabilities
    )
    os_command_enforced = os_command_exposed and os_qualifier == "os_ready"
    return (
        SecurityBoundarySignal(
            "path",
            "enforced"
            if os_command_enforced
            else "application_only"
            if path_tools
            else "absent",
            "os" if os_command_enforced else "application",
            "os_ready"
            if os_command_enforced
            else os_qualifier
            if path_tools
            else "application",
        ),
        SecurityBoundarySignal(
            "network",
            "enforced" if os_command_enforced else "absent",
            "os" if os_command_enforced else "application",
            "os_ready" if os_command_enforced else os_qualifier,
        ),
        SecurityBoundarySignal(
            "command",
            "restricted" if command_tools or os_command_exposed else "absent",
            "os" if os_command_enforced else "application",
            "os_ready" if os_command_enforced else "application",
        ),
        SecurityBoundarySignal(
            "audit",
            cast(SecurityBoundaryState, security.audit_status),
            "application",
            "application",
        ),
    )


def _security_boundary_risk(signal: SecurityBoundarySignal) -> int:
    if signal.key == "path":
        return {"enforced": 0, "absent": 0, "application_only": 1}[signal.state]
    if signal.key == "audit":
        return {
            "recording": 0,
            "busy": 1,
            "disabled": 1,
            "degraded": 2,
            "unavailable": 2,
        }[signal.state]
    return 0


def _security_boundary_alerts(
    signals: tuple[SecurityBoundarySignal, ...],
    changes: tuple[SecurityBoundaryChange, ...],
    *,
    observation_count: int,
    observation_failures: int,
) -> tuple[SecurityBoundaryAlert, ...]:
    candidates: list[SecurityBoundaryAlert] = []
    path = signals[0]
    if path.qualifier == "os_disabled":
        candidates.append(
            SecurityBoundaryAlert(
                "path",
                "os_disabled",
                "warning",
                observation_count,
            )
        )
    elif path.qualifier == "os_fail_closed":
        candidates.append(
            SecurityBoundaryAlert(
                "path",
                "os_fail_closed",
                "warning",
                observation_count,
            )
        )
    audit = signals[3]
    audit_alerts: dict[
        SecurityBoundaryState,
        tuple[SecurityAlertCode, SecurityAlertSeverity],
    ] = {
        "busy": ("audit_busy", "warning"),
        "disabled": ("audit_disabled", "warning"),
        "degraded": ("audit_degraded", "critical"),
        "unavailable": ("audit_unavailable", "critical"),
    }
    if audit.state in audit_alerts:
        code, severity = audit_alerts[audit.state]
        candidates.append(
            SecurityBoundaryAlert(
                "audit",
                code,
                severity,
                observation_count,
            )
        )
    for change in changes:
        if change.severity == "warning":
            candidates.append(
                SecurityBoundaryAlert(
                    change.after.key,
                    "boundary_downgrade",
                    "warning",
                    change.observation_index,
                )
            )
    if observation_failures:
        candidates.append(
            SecurityBoundaryAlert(
                "observer",
                "observation_failed",
                "critical",
                observation_count,
                observation_failures,
            )
        )

    aggregated: dict[
        tuple[SecurityAlertScope, SecurityAlertCode], SecurityBoundaryAlert
    ] = {}
    for alert in candidates:
        key = (alert.scope, alert.code)
        existing = aggregated.get(key)
        if existing is None:
            aggregated[key] = alert
            continue
        aggregated[key] = SecurityBoundaryAlert(
            alert.scope,
            alert.code,
            (
                "critical"
                if "critical" in {existing.severity, alert.severity}
                else "warning"
            ),
            max(existing.observation_index, alert.observation_index),
            existing.occurrences + alert.occurrences,
        )
    return tuple(
        sorted(
            aggregated.values(),
            key=lambda alert: (
                {"information": 0, "warning": 1, "critical": 2}[alert.severity],
                alert.observation_index,
                alert.scope,
                alert.code,
            ),
        )
    )


def observe_security_shield(
    tool_permissions: Mapping[str, bool],
    *,
    sandbox_backend: str,
    audit_enabled: bool,
    sandbox_probe: Callable[[], SandboxCapabilities] | None = None,
    audit_probe: Callable[[], AuditLogStatus] | None = None,
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
    audit_status: AuditBoundaryStatus = "disabled"
    if audit_enabled:
        audit_status = "recording"
        if audit_probe is not None:
            try:
                audit_observation = audit_probe()
            except (AuditError, OSError, RuntimeError, ValueError):
                audit_status = "unavailable"
            else:
                if not isinstance(audit_observation, AuditLogStatus):
                    audit_status = "unavailable"
                elif not audit_observation.lock_available:
                    audit_status = "busy"
                elif any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in (
                        audit_observation.current_records,
                        audit_observation.backup_records,
                        audit_observation.invalid_records,
                    )
                ):
                    audit_status = "unavailable"
                elif audit_observation.invalid_records:
                    audit_status = "degraded"
    return project_security_shield(
        tool_permissions,
        sandbox_backend=sandbox_backend,
        audit_enabled=audit_enabled,
        audit_status=audit_status,
        sandbox_capabilities=capabilities,
        sandbox_probe_failed=probe_failed,
    )


def project_security_shield(
    tool_permissions: Mapping[str, bool],
    *,
    sandbox_backend: str,
    audit_enabled: bool,
    audit_status: AuditBoundaryStatus | None = None,
    sandbox_capabilities: SandboxCapabilities | None = None,
    sandbox_probe_failed: bool = False,
) -> SecurityShield:
    """Project stable capability bands from already-observed runtime facts."""

    if sandbox_backend not in {"disabled", "windows-sandbox"}:
        raise ValueError("unknown sandbox backend")
    if type(audit_enabled) is not bool or type(sandbox_probe_failed) is not bool:
        raise ValueError("security observation flags must be boolean")
    resolved_audit_status: AuditBoundaryStatus = (
        "recording" if audit_enabled else "disabled"
    )
    if audit_status is not None:
        if audit_status not in _AUDIT_BOUNDARY_STATUSES:
            raise ValueError("security audit observation status is invalid")
        resolved_audit_status = audit_status
    if audit_enabled == (resolved_audit_status == "disabled"):
        raise ValueError("security audit configuration and status contradict")
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
        audit_status=resolved_audit_status,
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
                "only the approval-bound sandbox command tool may be exposed",
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
