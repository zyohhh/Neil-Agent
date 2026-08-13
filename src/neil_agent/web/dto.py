"""Versioned DTOs for the local Web Workbench."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


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
    wire_protocol: str = Field(min_length=1, max_length=64)
    thinking_enabled: bool
    capabilities: ProviderCapabilitiesDto


class GitFileDto(WorkbenchDto):
    path: str = Field(min_length=1, max_length=4_096)
    status: str = Field(min_length=1, max_length=8)
    kind: Literal["modified", "added", "deleted", "renamed", "untracked", "conflict"]


class GitDto(WorkbenchDto):
    available: bool
    branch: str | None = Field(default=None, max_length=512)
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


class SessionListDto(WorkbenchDto):
    available: bool
    items: tuple[SessionDto, ...] = Field(default=(), max_length=20)
    invalid_count: int = Field(default=0, ge=0)
    total_count: int = Field(default=0, ge=0)


class FileNodeDto(WorkbenchDto):
    name: str = Field(min_length=1, max_length=255)
    path: str = Field(max_length=4_096)
    kind: Literal["directory", "file"]
    children: tuple[FileNodeDto, ...] = Field(default=(), max_length=200)


class FileTreeDto(WorkbenchDto):
    root: str = Field(max_length=4_096)
    items: tuple[FileNodeDto, ...] = Field(default=(), max_length=200)
    truncated: bool = False


class TaskStepDto(WorkbenchDto):
    title: str = Field(min_length=1, max_length=200)
    status: Literal["pending", "in_progress", "completed"]


class TaskDto(WorkbenchDto):
    source: Literal["saved_session", "unavailable"]
    session_id: str | None = Field(default=None, max_length=128)
    steps: tuple[TaskStepDto, ...] = Field(default=(), max_length=5)


class ContextDto(WorkbenchDto):
    source: Literal["server_reported", "unavailable"]
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    limit_tokens: int | None = Field(default=None, ge=1_000)


class QualityCheckDto(WorkbenchDto):
    check: str = Field(min_length=1, max_length=200)
    status: Literal["passed", "failed", "not_run"]
    exit_code: int | None = None


class ReviewDto(WorkbenchDto):
    state: ReviewState
    git: GitDto
    quality_check: QualityCheckDto | None = None
    approval_available: bool = False
    cost_available: Literal[False] = False


class SecurityDto(WorkbenchDto):
    mode: Literal["approval_gated"] = "approval_gated"
    binding: Literal["loopback"] = "loopback"
    bootstrap_token_required: Literal[True] = True
    write_routes: Literal[0] = 0
    agent_connected: Literal[True] = True
    sandbox_backend: Literal["disabled", "windows-sandbox"]
    audit_enabled: bool


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
