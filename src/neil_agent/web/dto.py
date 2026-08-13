"""Versioned, metadata-only DTOs for the local Web Workbench."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class WorkbenchDto(BaseModel):
    """Strict base model shared by all Web Workbench responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class HealthDto(WorkbenchDto):
    status: Literal["ready"] = "ready"
    service: Literal["neil-agent-web"] = "neil-agent-web"
    schema_version: Literal[1] = 1
    read_only: Literal[True] = True


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
    state: Literal["empty", "passed", "failed", "stale", "unavailable"]
    git: GitDto
    quality_check: QualityCheckDto | None = None
    approval_available: Literal[False] = False
    cost_available: Literal[False] = False


class SecurityDto(WorkbenchDto):
    mode: Literal["read_only"] = "read_only"
    binding: Literal["loopback"] = "loopback"
    bootstrap_token_required: Literal[True] = True
    write_routes: Literal[0] = 0
    agent_connected: Literal[False] = False
    sandbox_backend: Literal["disabled", "windows-sandbox"]
    audit_enabled: bool


class RunDto(WorkbenchDto):
    status: Literal["not_connected"] = "not_connected"
    detail: Literal[
        "P1 exposes saved metadata only; Agent execution is not connected"
    ] = "P1 exposes saved metadata only; Agent execution is not connected"


class WorkbenchSnapshotDto(WorkbenchDto):
    schema_version: Literal[1] = 1
    source: Literal["live"] = "live"
    generated_at: AwareDatetime
    workspace: WorkspaceDto
    provider: ProviderDto
    run: RunDto = RunDto()
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
