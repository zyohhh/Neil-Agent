"""Versioned DTOs for the local Web Workbench."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from ..context import ContextTomography


class WorkbenchDto(BaseModel):
    """Strict base model shared by all Web Workbench responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")


RunStatus = Literal[
    "idle",
    "running",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
]
ReviewState = Literal[
    "empty",
    "passed",
    "failed",
    "approval_required",
    "stale",
    "applied",
    "unavailable",
]
StepStatus = Literal[
    "pending",
    "running",
    "waiting_for_approval",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
]


class HealthDto(WorkbenchDto):
    status: Literal["ready"] = "ready"
    service: Literal["neil-agent-web"] = "neil-agent-web"
    schema_version: Literal[1] = 1
    read_only_tools: Literal[True] = True
    realtime: Literal[True] = True


class WebSocketTicketDto(WorkbenchDto):
    ticket: str = Field(min_length=32, max_length=128)
    expires_in_seconds: Literal[30] = 30


class WorkspaceDto(WorkbenchDto):
    name: str = Field(min_length=1, max_length=255)
    identity: str = Field(pattern=r"^[0-9a-f]{16}$")


class ProviderCapabilitiesDto(WorkbenchDto):
    streaming: bool
    tool_calling: bool
    parallel_tool_calls: bool
    reasoning_state: bool
    usage_reporting: bool


class ProviderDto(WorkbenchDto):
    provider: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    available_models: tuple[str, ...] = Field(min_length=1, max_length=16)
    wire_protocol: str = Field(min_length=1, max_length=64)
    thinking_enabled: bool
    capabilities: ProviderCapabilitiesDto


class GitFileDto(WorkbenchDto):
    path: str = Field(min_length=1, max_length=4_096)
    previous_path: str | None = Field(default=None, min_length=1, max_length=4_096)
    status: str = Field(min_length=1, max_length=8)
    kind: Literal["modified", "added", "deleted", "renamed", "untracked", "conflict"]
    additions: int | None = Field(default=None, ge=0)
    deletions: int | None = Field(default=None, ge=0)
    diff_available: bool = False
    diff_reason: Literal["available", "untracked", "binary", "conflict", "unavailable"]


class GitDto(WorkbenchDto):
    available: bool
    branch: str | None = Field(default=None, max_length=512)
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    change_count: int = Field(default=0, ge=0, le=10_000)
    files: tuple[GitFileDto, ...] = Field(default=(), max_length=100)
    truncated: bool = False


class SessionDto(WorkbenchDto):
    session_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    updated_at: AwareDatetime
    round_count: int = Field(ge=0)
    preview: str = Field(max_length=200)
    has_plan: bool
    failed_check: bool
    has_compaction: bool
    runtime_provider: str | None = Field(default=None, min_length=1, max_length=64)
    runtime_model: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def runtime_binding_must_be_complete(self) -> Self:
        if (self.runtime_provider is None) != (self.runtime_model is None):
            raise ValueError("session runtime binding must be complete")
        return self


class SessionListDto(WorkbenchDto):
    available: bool
    items: tuple[SessionDto, ...] = Field(default=(), max_length=20)
    invalid_count: int = Field(default=0, ge=0)
    total_count: int = Field(default=0, ge=0)


class ActiveSessionDto(WorkbenchDto):
    session_id: str = Field(
        pattern=r"^\d{8}T\d{12}Z-[0-9a-f]{8}$",
        max_length=128,
    )
    title: str = Field(min_length=1, max_length=80)
    round_count: int = Field(ge=0)
    persistence_status: Literal["unsaved", "saved", "save_failed"]
    runtime_provider: str = Field(min_length=1, max_length=64)
    runtime_model: str = Field(min_length=1, max_length=256)


class FileNodeDto(WorkbenchDto):
    name: str = Field(min_length=1, max_length=255)
    path: str = Field(max_length=4_096)
    kind: Literal["directory", "file"]
    children: tuple[FileNodeDto, ...] = Field(default=(), max_length=200)


class FileTreeDto(WorkbenchDto):
    root: str = Field(max_length=4_096)
    items: tuple[FileNodeDto, ...] = Field(default=(), max_length=200)
    truncated: bool = False
    revision: str = Field(pattern=r"^[0-9a-f]{16}$")
    unchanged: bool = False


class TaskStepDto(WorkbenchDto):
    title: str = Field(min_length=1, max_length=200)
    status: Literal["pending", "in_progress", "completed"]


class TaskDto(WorkbenchDto):
    source: Literal["saved_session", "unavailable"]
    session_id: str | None = Field(default=None, max_length=128)
    steps: tuple[TaskStepDto, ...] = Field(default=(), max_length=5)


class ContextLayerDto(WorkbenchDto):
    kind: Literal[
        "system",
        "tool_schemas",
        "project_instructions",
        "selected_history",
        "current_chain",
    ]
    chars: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    item_count: int = Field(ge=0, le=10_000)


class ContextPressureDto(WorkbenchDto):
    level: Literal["safe", "warning", "critical", "exceeded"]
    limiting_dimension: Literal["characters", "tokens"]
    character_basis_points: int = Field(ge=0)
    token_basis_points: int | None = Field(default=None, ge=0)
    character_headroom: int = Field(ge=0)
    token_headroom: int | None = Field(default=None, ge=0)


class ContextToolFootprintDto(WorkbenchDto):
    ordinal: int = Field(ge=1, le=10_000)
    chars: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    state: Literal["kept", "omitted"]


class ContextDto(WorkbenchDto):
    source: Literal["local_estimate", "unavailable"]
    tomography_schema_version: Literal[2] = 2
    budget_chars: int | None = Field(default=None, ge=1)
    limit_tokens: int | None = Field(default=None, ge=1_000)
    estimated_chars: int | None = Field(default=None, ge=0)
    estimated_tokens: int | None = Field(default=None, ge=0)
    stored_rounds: int | None = Field(default=None, ge=0, le=10_000)
    selected_rounds: int | None = Field(default=None, ge=0, le=10_000)
    omitted_rounds: int | None = Field(default=None, ge=0, le=10_000)
    stored_history_chars: int | None = Field(default=None, ge=0)
    omitted_history_chars: int | None = Field(default=None, ge=0)
    checkpoint_state: Literal["none", "kept", "omitted"] | None = None
    layers: tuple[ContextLayerDto, ...] = Field(default=(), max_length=5)
    pressure: ContextPressureDto | None = None
    largest_tool_footprint: ContextToolFootprintDto | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)

    @classmethod
    def from_tomography(cls, tomography: ContextTomography) -> ContextDto:
        """Map one ContextTomography snapshot into the Web Workbench context DTO."""

        from ..context import ContextTomography as ObservedTomography
        from ..context import context_budget_pressure

        if not isinstance(tomography, ObservedTomography):
            raise ValueError("context tomography observation is invalid")
        pressure = context_budget_pressure(tomography)
        largest = (
            tomography.tool_results.largest[0]
            if tomography.tool_results.largest
            else None
        )
        usage = tomography.last_server_usage
        return cls(
            source="local_estimate",
            budget_chars=tomography.budget_chars,
            limit_tokens=tomography.budget_tokens,
            estimated_chars=tomography.estimated_chars,
            estimated_tokens=tomography.estimated_tokens,
            stored_rounds=tomography.stored_rounds,
            selected_rounds=tomography.selected_rounds,
            omitted_rounds=tomography.omitted_rounds,
            stored_history_chars=tomography.stored_history_chars,
            omitted_history_chars=tomography.omitted_history_chars,
            checkpoint_state=tomography.checkpoint_state,
            layers=tuple(
                ContextLayerDto(
                    kind=layer.kind,
                    chars=layer.chars,
                    estimated_tokens=layer.estimated_tokens,
                    item_count=layer.item_count,
                )
                for layer in tomography.layers
            ),
            pressure=ContextPressureDto(
                level=pressure.level,
                limiting_dimension=pressure.limiting_dimension,
                character_basis_points=pressure.character_basis_points,
                token_basis_points=pressure.token_basis_points,
                character_headroom=pressure.character_headroom,
                token_headroom=pressure.token_headroom,
            ),
            largest_tool_footprint=(
                None
                if largest is None
                else ContextToolFootprintDto(
                    ordinal=largest.ordinal,
                    chars=largest.chars,
                    estimated_tokens=largest.estimated_tokens,
                    state=largest.state,
                )
            ),
            input_tokens=None if usage is None else usage.input_tokens,
            output_tokens=None if usage is None else usage.output_tokens,
            total_tokens=None if usage is None else usage.total_tokens,
        )


class QualityCheckDto(WorkbenchDto):
    check: str = Field(min_length=1, max_length=200)
    status: Literal["passed", "failed", "not_run"]
    exit_code: int | None = None


class CostEstimateDto(WorkbenchDto):
    source: Literal["versioned_rate_table", "unavailable"]
    estimated_usd: str | None = Field(default=None, pattern=r"^(0|[1-9]\d*)\.\d{6}$")
    rate_table_version: str | None = Field(default=None, max_length=64)
    rate_effective_date: str | None = Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    model: str | None = Field(default=None, max_length=256)
    reason: Literal[
        "no_rate_table",
        "no_saved_usage",
        "model_not_listed",
        "cache_rate_missing",
        "rate_not_effective",
        "estimated",
    ]


class ReviewDto(WorkbenchDto):
    state: ReviewState
    git: GitDto
    quality_check: QualityCheckDto | None = None
    quality_checks: tuple[QualityCheckDto, ...] = Field(default=(), max_length=20)
    approval_available: bool = False
    cost_available: bool = False
    cost: CostEstimateDto


class GitDiffDto(WorkbenchDto):
    path: str = Field(min_length=1, max_length=4_096)
    previous_path: str | None = Field(default=None, min_length=1, max_length=4_096)
    revision: str = Field(pattern=r"^[0-9a-f]{16}$")
    available: bool
    reason: Literal[
        "available", "untracked", "binary", "conflict", "empty", "stale", "unavailable"
    ]
    content: str = Field(default="", max_length=40_000)
    truncated: bool = False


class SecurityDto(WorkbenchDto):
    mode: Literal["approval_gated"] = "approval_gated"
    binding: Literal["loopback"] = "loopback"
    bootstrap_token_required: Literal[True] = True
    write_routes: Literal[0] = 0
    agent_connected: Literal[True] = True
    sandbox_backend: Literal["disabled", "windows-sandbox"]
    audit_enabled: bool
    shield_schema_version: int = Field(ge=1)
    application_status: Literal[
        "enforced", "ready", "disabled", "incomplete", "unavailable"
    ]
    os_sandbox_status: Literal[
        "enforced", "ready", "disabled", "incomplete", "unavailable"
    ]
    audit_status: Literal["recording", "busy", "disabled", "degraded", "unavailable"]
    tool_count: int = Field(ge=0)
    direct_tool_count: int = Field(ge=0)
    approval_tool_count: int = Field(ge=0)


class RunDto(WorkbenchDto):
    status: RunStatus = "idle"
    run_id: str | None = Field(default=None, pattern=r"^run-[0-9a-f]{32}$")
    objective: str | None = Field(default=None, max_length=500)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error_type: str | None = Field(default=None, max_length=120)


class RuntimeStepDto(WorkbenchDto):
    correlation_id: str = Field(min_length=1, max_length=80)
    stage: Literal[
        "agent_turn", "model_request", "tool_call", "approval", "quality_check"
    ]
    title: str = Field(min_length=1, max_length=200)
    status: StepStatus
    timestamp: AwareDatetime
    metadata: dict[str, bool | int | str] = Field(default_factory=dict)


class OutputEntryDto(WorkbenchDto):
    kind: Literal["status", "activity", "assistant", "warning", "error"]
    text: str = Field(min_length=1, max_length=4_000)
    timestamp: AwareDatetime


class ApprovalRequestDto(WorkbenchDto):
    request_id: str = Field(pattern=r"^approval-[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^run-[0-9a-f]{32}$")
    tool_name: str = Field(min_length=1, max_length=128)
    preview: str = Field(min_length=1, max_length=30_000)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    state: Literal["pending", "approved", "rejected", "expired", "stale"]
    decision_detail: str | None = Field(default=None, max_length=240)


class RuntimeCapabilitiesDto(WorkbenchDto):
    can_start_turn: bool
    can_cancel_turn: bool
    can_request_control: bool = True
    can_approve_tool: bool
    can_show_diff: bool = True
    can_estimate_cost: bool = False
    can_create_session: bool = True
    can_select_session: bool = True
    can_switch_model: bool = False
    tool_permission_mode: Literal["approval_gated"] = "approval_gated"
    has_pty: Literal[False] = False


class WorkbenchSnapshotDto(WorkbenchDto):
    schema_version: Literal[1] = 1
    source: Literal["live"] = "live"
    generated_at: AwareDatetime
    workspace: WorkspaceDto
    provider: ProviderDto
    run: RunDto = RunDto()
    revision: int = Field(default=0, ge=0)
    last_sequence: int = Field(default=0, ge=0)
    capabilities: RuntimeCapabilitiesDto = RuntimeCapabilitiesDto(
        can_start_turn=True,
        can_cancel_turn=False,
        can_approve_tool=False,
    )
    timeline: tuple[RuntimeStepDto, ...] = Field(default=(), max_length=200)
    output: tuple[OutputEntryDto, ...] = Field(default=(), max_length=200)
    approval: ApprovalRequestDto | None = None
    git: GitDto
    sessions: SessionListDto
    active_session: ActiveSessionDto | None = None
    files: FileTreeDto
    task: TaskDto
    context: ContextDto
    review: ReviewDto
    security: SecurityDto


def utc_timestamp(value: datetime) -> datetime:
    """Return one aware timestamp for DTO construction."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Workbench timestamps must be timezone-aware")
    return value
