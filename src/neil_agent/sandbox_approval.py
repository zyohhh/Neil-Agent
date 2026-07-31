"""Stable approval identity for a future shell-free sandbox command tool.

This module only models and renders an approval.  It deliberately does not
register ``run_command`` or make the Windows Sandbox backend ready.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    model_validator,
)

from .approval import (
    SANDBOX_APPROVAL_BINDING_KIND,
    ApprovalBinding,
)
from .sandbox_guest import (
    GUEST_PROTOCOL_VERSION,
    GUEST_RUNNER_VERSION,
    MAX_ACTIVE_PROCESSES,
    MAX_ARGUMENTS,
    MAX_JOB_MEMORY_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_PROCESS_MEMORY_BYTES,
    MAX_TIMEOUT_MS,
    MIN_MEMORY_BYTES,
    MIN_OUTPUT_BYTES,
    MIN_TIMEOUT_MS,
    SandboxGuestRequest,
)

RUN_COMMAND_APPROVAL_BINDING_VERSION: Literal[1] = 1
WINDOWS_SANDBOX_BACKEND_VERSION: Literal[1] = 1
WINDOWS_SANDBOX_BACKEND: Literal["windows-sandbox"] = "windows-sandbox"
NETWORK_POLICY: Literal["deny"] = "deny"
WORKSPACE_POLICY: Literal["read-only-snapshot"] = "read-only-snapshot"
ENVIRONMENT_POLICY: Literal["fixed-empty"] = "fixed-empty"
MODIFICATION_POLICY: Literal["discard-all"] = "discard-all"
_BINDING_DOMAIN = b"neil-agent:sandbox-run-command-approval:v1\0"


class RunCommandApprovalBinding(BaseModel):
    """Stable semantics shown to a user before one candidate sandbox run."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    version: Literal[1] = RUN_COMMAND_APPROVAL_BINDING_VERSION
    executable: StrictStr
    argv: tuple[StrictStr, ...] = Field(max_length=MAX_ARGUMENTS)
    logical_cwd: StrictStr = "."
    snapshot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_binary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    backend: Literal["windows-sandbox"] = WINDOWS_SANDBOX_BACKEND
    backend_version: Literal[1] = WINDOWS_SANDBOX_BACKEND_VERSION
    guest_protocol_version: Literal[2] = GUEST_PROTOCOL_VERSION
    runner_version: Literal[2] = GUEST_RUNNER_VERSION
    network_policy: Literal["deny"] = NETWORK_POLICY
    workspace_policy: Literal["read-only-snapshot"] = WORKSPACE_POLICY
    environment_policy: Literal["fixed-empty"] = ENVIRONMENT_POLICY
    modification_policy: Literal["discard-all"] = MODIFICATION_POLICY
    timeout_ms: StrictInt = Field(ge=MIN_TIMEOUT_MS, le=MAX_TIMEOUT_MS)
    max_output_bytes: StrictInt = Field(
        ge=MIN_OUTPUT_BYTES,
        le=MAX_OUTPUT_BYTES,
    )
    active_process_limit: StrictInt = Field(ge=1, le=MAX_ACTIVE_PROCESSES)
    process_memory_bytes: StrictInt = Field(
        ge=MIN_MEMORY_BYTES,
        le=MAX_PROCESS_MEMORY_BYTES,
    )
    job_memory_bytes: StrictInt = Field(
        ge=MIN_MEMORY_BYTES,
        le=MAX_JOB_MEMORY_BYTES,
    )

    @model_validator(mode="after")
    def validate_guest_semantics(self) -> RunCommandApprovalBinding:
        """Reuse the fixed guest request's path, argv, and limit validation."""

        SandboxGuestRequest.create(
            run_id="0" * 32,
            instance_id="1" * 32,
            snapshot_manifest_sha256=self.snapshot_manifest_sha256,
            runner_source_sha256=self.runner_source_sha256,
            approval_binding_sha256=self.digest,
            executable=self.executable,
            argv=self.argv,
            cwd=self.logical_cwd,
            environment={},
            timeout_ms=self.timeout_ms,
            max_output_bytes=self.max_output_bytes,
            active_process_limit=self.active_process_limit,
            process_memory_bytes=self.process_memory_bytes,
            job_memory_bytes=self.job_memory_bytes,
        )
        return self

    def canonical_bytes(self) -> bytes:
        """Serialize the stable, non-ephemeral approval fields canonically."""

        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        """Return a domain-separated digest for ApprovalRequest v2."""

        return sha256(_BINDING_DOMAIN + self.canonical_bytes()).hexdigest()

    @property
    def approval_binding(self) -> ApprovalBinding:
        """Return the generic triple persisted by :class:`ApprovalStore`."""

        return ApprovalBinding(
            kind=SANDBOX_APPROVAL_BINDING_KIND,
            version=self.version,
            sha256=self.digest,
        )

    def render_preview(self) -> str:
        """Render every bound field without shell-style ambiguous quoting."""

        argv_json = json.dumps(
            list(self.argv),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        executable_json = json.dumps(self.executable, ensure_ascii=False)
        cwd_json = json.dumps(self.logical_cwd, ensure_ascii=False)
        return "\n".join(
            (
                "Windows Sandbox command approval",
                f"Executable (snapshot-relative): {executable_json}",
                f"Argv (canonical JSON): {argv_json}",
                f"Logical cwd: {cwd_json}",
                f"Snapshot manifest SHA-256: {self.snapshot_manifest_sha256}",
                f"Runner source SHA-256: {self.runner_source_sha256}",
                f"Runner binary SHA-256: {self.runner_binary_sha256}",
                f"Approval binding version: {self.version}",
                f"Backend: {self.backend}",
                f"Backend version: {self.backend_version}",
                f"Guest protocol version: {self.guest_protocol_version}",
                f"Runner version: {self.runner_version}",
                f"Network policy: {self.network_policy}",
                f"Workspace policy: {self.workspace_policy}",
                f"Environment policy: {self.environment_policy}",
                f"Modification policy: {self.modification_policy}",
                f"Timeout (ms): {self.timeout_ms}",
                f"Maximum output (bytes): {self.max_output_bytes}",
                f"Active process limit: {self.active_process_limit}",
                f"Process memory limit (bytes): {self.process_memory_bytes}",
                f"Job memory limit (bytes): {self.job_memory_bytes}",
                f"Approval-Binding-SHA256: {self.digest}",
            )
        )
