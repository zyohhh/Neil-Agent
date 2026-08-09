"""Strict target-platform evidence for the Windows Sandbox candidate.

This module deliberately does not change backend readiness and does not register
an Agent tool.  It has three separate responsibilities:

* collect one canonical evidence record from bounded workflow inputs;
* verify at least three complete, internally consistent real-platform repeats;
* issue a certification only when an independent review is explicitly pinned.

An aggregate proves that the supplied test evidence is structurally complete.
It is not a certification.  In particular, the default empty
:class:`ReviewTrustPins` can never issue or verify a certification.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from threading import Lock
from typing import Annotated, Any, Literal, TypeVar, cast
from uuid import UUID
from xml.etree import ElementTree

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

EVIDENCE_VERSION: Literal[1] = 1
CERTIFICATION_VERSION: Literal[1] = 1
MINIMUM_EVIDENCE_REPEATS = 3
EXPECTED_EVIDENCE_REPEAT_IDS = ("repeat-1", "repeat-2", "repeat-3")
MAX_EVIDENCE_JSON_BYTES = 4 * 1024 * 1024
MAX_JUNIT_XML_BYTES = 4 * 1024 * 1024
MAX_RAW_CLI_RESPONSE_BYTES = 64 * 1024
MAX_RAW_OBSERVATIONS = 512
MAX_RAW_OBSERVATION_JSONL_BYTES = 32 * 1024 * 1024
MAX_IDENTITY_FILE_BYTES = 128 * 1024 * 1024
MAX_PLATFORM_COMMAND_OUTPUT_BYTES = 64 * 1024
MAX_ATTESTATION_BYTES = 16 * 1024 * 1024
MAX_CERTIFICATION_LIFETIME = timedelta(days=90)
MAX_EVIDENCE_TO_REVIEW_DELAY = timedelta(days=7)
MAX_EVIDENCE_VALIDITY = timedelta(days=90)
CERTIFIED_SECURITY_ASSURANCE = "certified-windows-sandbox-v1"
BACKEND_POLICY_VERSION: Literal[1] = 1
ATTESTED_GITHUB_REPOSITORY = "zyohhh/Neil-Agent"
ATTESTED_GITHUB_WORKFLOW = (
    "github.com/zyohhh/Neil-Agent/.github/workflows/windows-sandbox-security.yml"
)
ATTESTED_GITHUB_REF = "refs/heads/main"
REQUIRED_SECURITY_GATE_IDS = tuple(
    sorted(
        (
            "actions-provenance",
            "broker-escape-denial",
            "cancellation",
            "guest-low-integrity-token",
            "job-memory-limit",
            "network-denial",
            "output-limit",
            "process-memory-limit",
            "process-tree-cleanup",
            "process-count-limit",
            "result-integrity",
            "snapshot-integrity",
            "source-binding",
            "timeout",
        )
    )
)

REQUIRED_WINDOWS_SANDBOX_TESTS = tuple(
    sorted(
        (
            "tests/test_sandbox_guest.py"
            "::test_real_runner_reclassifies_fast_flood_after_child_exit",
            "tests/test_sandbox_guest.py"
            "::test_real_runner_cancels_only_after_guest_tree_is_ready",
            "tests/test_sandbox_lease.py"
            "::test_sealed_lease_blocks_write_delete_and_rename",
            "tests/test_sandbox_snapshot.py"
            "::test_windows_directory_guard_blocks_real_junction_replacement",
            "tests/test_windows_sandbox_security.py"
            "::test_real_wsb_blocks_host_files_network_and_workspace_writeback",
            "tests/test_windows_sandbox_security.py"
            "::test_real_wsb_bounds_output_while_it_is_read",
            "tests/test_windows_sandbox_security.py"
            "::test_real_wsb_enforces_active_process_limit",
            "tests/test_windows_sandbox_security.py"
            "::test_real_wsb_enforces_process_memory_limit",
            "tests/test_windows_sandbox_security.py"
            "::test_real_wsb_enforces_aggregate_job_memory_limit",
            "tests/test_windows_sandbox_security.py"
            "::test_real_wsb_host_cancellation_stops_the_explicit_instance",
            "tests/test_windows_sandbox_security.py"
            "::test_real_wsb_job_denies_breakaway_process_creation",
            "tests/test_windows_sandbox_security.py"
            "::test_real_wsb_kills_child_and_grandchild_on_timeout",
            "tests/test_windows_sandbox_security.py"
            "::test_real_wsb_restricts_low_integrity_child_and_protects_runner_result",
            "tests/test_windows_sandbox_security.py"
            "::test_real_wsb_blocks_scm_task_scheduler_and_wmi_broker_escape",
        )
    )
)

REQUIRED_CLI_SCHEMA_STAGES = (
    "start",
    "runner",
    "share",
    "exporter",
    "stop",
    "list_after_stop",
)

REQUIRED_SUBJECT_SOURCE_PATHS = (
    "pyproject.toml",
    "src/neil_agent/approval.py",
    "src/neil_agent/cli.py",
    "src/neil_agent/config.py",
    "src/neil_agent/diagnostics.py",
    "src/neil_agent/noninteractive.py",
    "src/neil_agent/sandbox.py",
    "src/neil_agent/sandbox_approval.py",
    "src/neil_agent/sandbox_evidence.py",
    "src/neil_agent/sandbox_guest.py",
    "src/neil_agent/sandbox_guest_runner.cs",
    "src/neil_agent/sandbox_lease.py",
    "src/neil_agent/sandbox_runtime.py",
    "src/neil_agent/sandbox_snapshot.py",
    "src/neil_agent/security.py",
    "src/neil_agent/tools/registry.py",
    "src/neil_agent/tools/sandbox.py",
    "src/neil_agent/windows_sandbox.py",
    "tests/fixtures/sandbox_security_probe.cs",
    "tests/test_sandbox_guest.py",
    "tests/test_sandbox_lease.py",
    "tests/test_sandbox_runtime.py",
    "tests/test_sandbox_snapshot.py",
    "tests/test_windows_sandbox_security.py",
)

Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
GitObjectId = Annotated[StrictStr, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
SafeIdentifier = Annotated[
    StrictStr,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$"),
]
SchemaStage = Literal[
    "start",
    "runner",
    "share",
    "exporter",
    "stop",
    "list_after_stop",
]
SchemaRootType = Literal["object", "array"]
SchemaValueType = Literal["string", "integer", "boolean", "null", "object", "array"]
ExecutionNonce = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")]
CanonicalUuid = Annotated[
    StrictStr,
    Field(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        )
    ),
]

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class SandboxEvidenceError(ValueError):
    """Evidence was incomplete, inconsistent, untrusted, or non-canonical."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RequiredTestManifest(_StrictModel):
    """The immutable attack-test set required by this source revision."""

    version: Literal[1] = EVIDENCE_VERSION
    nodeids: tuple[StrictStr, ...] = Field(min_length=1, max_length=128)
    manifest_sha256: Digest

    @field_validator("nodeids")
    @classmethod
    def nodeids_are_sorted_unique_and_safe(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("required test node IDs must be sorted and unique")
        if any(not _is_safe_nodeid(nodeid) for nodeid in value):
            raise ValueError("required test manifest contains an unsafe node ID")
        return value

    @model_validator(mode="after")
    def digest_matches_payload(self) -> RequiredTestManifest:
        if self.manifest_sha256 != _digest_payload(self.hash_payload()):
            raise ValueError("required test manifest digest does not match")
        return self

    def hash_payload(self) -> dict[str, object]:
        return {
            "version": self.version,
            "nodeids": list(self.nodeids),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_model_bytes(self)


def required_test_manifest() -> RequiredTestManifest:
    """Return the one test manifest accepted by this source revision."""

    hash_payload: dict[str, object] = {
        "version": EVIDENCE_VERSION,
        "nodeids": list(REQUIRED_WINDOWS_SANDBOX_TESTS),
    }
    return RequiredTestManifest.model_validate(
        {
            "version": EVIDENCE_VERSION,
            "nodeids": REQUIRED_WINDOWS_SANDBOX_TESTS,
            "manifest_sha256": _digest_payload(hash_payload),
        }
    )


class PlatformFingerprint(_StrictModel):
    """Non-secret OS and WSB identity captured on the security runner."""

    version: Literal[1] = EVIDENCE_VERSION
    os_product_name: StrictStr = Field(min_length=1, max_length=128)
    edition_id: StrictStr = Field(min_length=1, max_length=128)
    display_version: StrictStr = Field(min_length=1, max_length=64)
    build_number: StrictInt = Field(ge=1)
    ubr: StrictInt = Field(ge=0)
    architecture: Literal["AMD64", "ARM64"]
    sandbox_feature_state: Literal["Enabled"]
    wsb_executable_name: Literal["wsb.exe"]
    wsb_file_version: StrictStr = Field(min_length=1, max_length=128)
    wsb_product_version: StrictStr = Field(min_length=1, max_length=128)
    wsb_sha256: Digest
    authenticode_status: Literal["Valid"]
    signer_thumbprint: StrictStr = Field(pattern=r"^[0-9a-f]{40,128}$")

    @field_validator(
        "os_product_name",
        "edition_id",
        "display_version",
        "wsb_file_version",
        "wsb_product_version",
    )
    @classmethod
    def text_has_no_controls(cls, value: str) -> str:
        if _CONTROL_CHARACTERS.search(value):
            raise ValueError("platform fingerprint text contains control characters")
        return value

    @model_validator(mode="after")
    def target_platform_is_supported(self) -> PlatformFingerprint:
        supported_editions = {
            "Enterprise",
            "EnterpriseN",
            "Professional",
            "ProfessionalN",
            "ProfessionalWorkstation",
            "ProfessionalWorkstationN",
        }
        if self.edition_id not in supported_editions:
            raise ValueError(
                "evidence runner must use a supported Pro or Enterprise edition"
            )
        if self.build_number < 26_100:
            raise ValueError(
                "evidence runner must use Windows 11 24H2 build 26100 or newer"
            )
        return self


class EvidenceSubject(_StrictModel):
    """Exact code, binaries, policy, and tests exercised by one run."""

    version: Literal[1] = EVIDENCE_VERSION
    git_commit_sha: GitObjectId
    source_manifest_sha256: Digest
    wheel_sha256: Digest
    uv_lock_sha256: Digest
    workflow_sha256: Digest
    runner_source_sha256: Digest
    runner_binary_sha256: Digest
    compiler_sha256: Digest
    framework_reference_sha256: Digest
    probe_source_sha256: Digest
    probe_binary_sha256: Digest
    required_test_manifest_sha256: Digest
    backend_policy_version: StrictInt = Field(ge=1)
    guest_protocol_version: StrictInt = Field(ge=1)
    runner_version: StrictInt = Field(ge=1)
    security_assurance: StrictStr = Field(min_length=1, max_length=128)
    actions_runner_version: SafeIdentifier
    actions_runner_ephemeral: Literal[True]

    @field_validator("security_assurance")
    @classmethod
    def assurance_is_safe_text(cls, value: str) -> str:
        if _CONTROL_CHARACTERS.search(value):
            raise ValueError("security assurance contains control characters")
        return value


def collect_windows_platform_fingerprint(
    wsb_executable: Path,
) -> PlatformFingerprint:
    """Capture an independently verifiable Windows/WSB identity.

    The fixed PowerShell script receives no untrusted command text.  It asks
    Windows for the optional-feature state and Authenticode result, while this
    process independently hashes the exact executable and rejects a race.
    """

    if os.name != "nt":
        raise SandboxEvidenceError("platform fingerprint collection requires Windows")
    wsb_path = _require_absolute_path(wsb_executable, "WSB executable")
    wsb_sha256 = _hash_identity_file(
        wsb_path,
        label="WSB executable",
        allow_hardlinks=True,
    )
    system_root_value = os.environ.get("SystemRoot")
    if not system_root_value:
        raise SandboxEvidenceError("SystemRoot is unavailable")
    system_root = _require_absolute_path(Path(system_root_value), "SystemRoot")
    powershell = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    _hash_identity_file(
        powershell,
        label="Windows PowerShell",
        allow_hardlinks=True,
    )
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$path = [Environment]::GetEnvironmentVariable('NEIL_EVIDENCE_WSB_PATH')
$item = Get-Item -LiteralPath $path -Force
$signature = Get-AuthenticodeSignature -LiteralPath $path
$windows = Get-ItemProperty -LiteralPath `
  'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$feature = Dism\Get-WindowsOptionalFeature `
  -Online -FeatureName 'Containers-DisposableClientVM'
$thumbprint = if ($null -eq $signature.SignerCertificate) {
  ''
} else {
  $signature.SignerCertificate.Thumbprint.ToLowerInvariant()
}
[ordered]@{
  version = 1
  os_product_name = [string]$windows.ProductName
  edition_id = [string]$windows.EditionID
  display_version = [string]$windows.DisplayVersion
  build_number = [int]$windows.CurrentBuildNumber
  ubr = [int]$windows.UBR
  architecture = [string]$env:PROCESSOR_ARCHITECTURE
  sandbox_feature_state = [string]$feature.State
  wsb_executable_name = $item.Name.ToLowerInvariant()
  wsb_file_version = [string]$item.VersionInfo.FileVersion
  wsb_product_version = [string]$item.VersionInfo.ProductVersion
  wsb_sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
  authenticode_status = [string]$signature.Status
  signer_thumbprint = $thumbprint
} | ConvertTo-Json -Compress
"""
    system32 = system_root / "System32"
    environment = {
        "SystemRoot": str(system_root),
        "WINDIR": str(system_root),
        "PATH": os.pathsep.join(
            (
                str(system32),
                str(system_root),
                str(system32 / "WindowsPowerShell" / "v1.0"),
            )
        ),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PSModulePath": str(system32 / "WindowsPowerShell" / "v1.0" / "Modules"),
        "TEMP": os.environ.get("TEMP", str(system_root / "Temp")),
        "TMP": os.environ.get("TMP", str(system_root / "Temp")),
        "PROCESSOR_ARCHITECTURE": os.environ.get(
            "PROCESSOR_ARCHITECTURE",
            "",
        ),
        "NEIL_EVIDENCE_WSB_PATH": str(wsb_path),
    }
    try:
        completed = subprocess.run(
            (
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ),
            shell=False,
            cwd=system32,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SandboxEvidenceError(
            "platform fingerprint command failed closed"
        ) from error
    if (
        completed.returncode != 0
        or not completed.stdout
        or len(completed.stdout) > MAX_PLATFORM_COMMAND_OUTPUT_BYTES
        or len(completed.stderr) > MAX_PLATFORM_COMMAND_OUTPUT_BYTES
        or completed.stderr.strip()
    ):
        raise SandboxEvidenceError(
            "platform fingerprint command returned an invalid result"
        )
    payload = _strict_json_value(completed.stdout)
    if not isinstance(payload, dict):
        raise SandboxEvidenceError(
            "platform fingerprint command did not return an object"
        )
    try:
        fingerprint = PlatformFingerprint.model_validate(payload, strict=True)
    except ValueError as error:
        raise SandboxEvidenceError(
            "platform fingerprint did not match the strict schema"
        ) from error
    if fingerprint.wsb_sha256 != wsb_sha256:
        raise SandboxEvidenceError(
            "WSB executable changed during platform fingerprint collection"
        )
    return fingerprint


def collect_evidence_subject(
    *,
    repository_root: Path,
    git_commit_sha: str,
    wheel_path: Path,
    runner_source_path: Path,
    runner_binary_path: Path,
    compiler_path: Path,
    probe_binary_path: Path,
    actions_runner_version: str,
    actions_runner_ephemeral: bool,
) -> EvidenceSubject:
    """Bind evidence to the exact reviewed source and built executables."""

    if actions_runner_ephemeral is not True:
        raise SandboxEvidenceError("evidence requires an ephemeral Actions runner")

    from .sandbox_guest import (
        GUEST_PROTOCOL_VERSION,
        GUEST_RUNNER_SECURITY_ASSURANCE,
        GUEST_RUNNER_VERSION,
    )

    root = _require_absolute_path(repository_root, "repository root")
    if not root.is_dir():
        raise SandboxEvidenceError("repository root is not a directory")
    source_records: list[dict[str, str]] = []
    for relative_text in REQUIRED_SUBJECT_SOURCE_PATHS:
        relative = Path(relative_text)
        candidate = root.joinpath(*relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise SandboxEvidenceError(
                f"required subject source is unavailable: {relative_text}"
            ) from error
        source_records.append(
            {
                "path": relative.as_posix(),
                "sha256": _hash_identity_file(
                    resolved,
                    label=f"subject source {relative_text}",
                ),
            }
        )

    repository_runner_source = root / "src" / "neil_agent" / "sandbox_guest_runner.cs"
    runner_source_sha256 = _hash_identity_file(
        runner_source_path,
        label="built runner source",
    )
    if runner_source_sha256 != _hash_identity_file(
        repository_runner_source,
        label="repository runner source",
    ):
        raise SandboxEvidenceError(
            "built runner source does not match the reviewed repository source"
        )
    compiler = _require_absolute_path(compiler_path, "C# compiler")
    framework_reference = compiler.parent / "System.Web.Extensions.dll"
    workflow_path = root / ".github" / "workflows" / "windows-sandbox-security.yml"
    uv_lock_path = root / "uv.lock"
    probe_source = root / "tests" / "fixtures" / "sandbox_security_probe.cs"
    return EvidenceSubject(
        git_commit_sha=git_commit_sha,
        source_manifest_sha256=_digest_payload(source_records),
        wheel_sha256=_hash_identity_file(wheel_path, label="built wheel"),
        uv_lock_sha256=_hash_identity_file(uv_lock_path, label="uv.lock"),
        workflow_sha256=_hash_identity_file(
            workflow_path,
            label="security workflow",
        ),
        runner_source_sha256=runner_source_sha256,
        runner_binary_sha256=_hash_identity_file(
            runner_binary_path,
            label="built runner binary",
        ),
        compiler_sha256=_hash_identity_file(
            compiler,
            label="C# compiler",
            allow_hardlinks=True,
        ),
        framework_reference_sha256=_hash_identity_file(
            framework_reference,
            label=".NET Framework reference",
            allow_hardlinks=True,
        ),
        probe_source_sha256=_hash_identity_file(
            probe_source,
            label="security probe source",
        ),
        probe_binary_sha256=_hash_identity_file(
            probe_binary_path,
            label="security probe binary",
        ),
        required_test_manifest_sha256=required_test_manifest().manifest_sha256,
        backend_policy_version=BACKEND_POLICY_VERSION,
        guest_protocol_version=GUEST_PROTOCOL_VERSION,
        runner_version=GUEST_RUNNER_VERSION,
        security_assurance=GUEST_RUNNER_SECURITY_ASSURANCE,
        actions_runner_version=actions_runner_version,
        actions_runner_ephemeral=cast(Literal[True], actions_runner_ephemeral),
    )


def verify_evidence_subject_checkout(
    subject: EvidenceSubject,
    repository_root: Path,
    git_commit_sha: str,
) -> None:
    """Bind a certified subject back to the exact current source checkout."""

    from .sandbox_guest import (
        GUEST_PROTOCOL_VERSION,
        GUEST_RUNNER_SECURITY_ASSURANCE,
        GUEST_RUNNER_VERSION,
    )

    root = _require_absolute_path(repository_root, "repository root")
    if subject.git_commit_sha != git_commit_sha:
        raise SandboxEvidenceError("current Git commit does not match evidence")
    source_records: list[dict[str, str]] = []
    for relative_text in REQUIRED_SUBJECT_SOURCE_PATHS:
        relative = Path(relative_text)
        candidate = root.joinpath(*relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise SandboxEvidenceError(
                f"current subject source is unavailable: {relative_text}"
            ) from error
        source_records.append(
            {
                "path": relative.as_posix(),
                "sha256": _hash_identity_file(
                    resolved,
                    label=f"current subject source {relative_text}",
                ),
            }
        )
    if _digest_payload(source_records) != subject.source_manifest_sha256:
        raise SandboxEvidenceError("current source manifest does not match evidence")
    current_bindings = (
        (
            root / ".github" / "workflows" / "windows-sandbox-security.yml",
            subject.workflow_sha256,
            "current security workflow",
        ),
        (root / "uv.lock", subject.uv_lock_sha256, "current uv.lock"),
        (
            root / "src" / "neil_agent" / "sandbox_guest_runner.cs",
            subject.runner_source_sha256,
            "current runner source",
        ),
        (
            root / "tests" / "fixtures" / "sandbox_security_probe.cs",
            subject.probe_source_sha256,
            "current security probe source",
        ),
    )
    for path, expected, label in current_bindings:
        if _hash_identity_file(path, label=label) != expected:
            raise SandboxEvidenceError(f"{label} does not match evidence")
    if (
        subject.required_test_manifest_sha256
        != required_test_manifest().manifest_sha256
        or subject.backend_policy_version != BACKEND_POLICY_VERSION
        or subject.guest_protocol_version != GUEST_PROTOCOL_VERSION
        or subject.runner_version != GUEST_RUNNER_VERSION
        or subject.security_assurance != GUEST_RUNNER_SECURITY_ASSURANCE
        or not subject.actions_runner_ephemeral
    ):
        raise SandboxEvidenceError(
            "current protocol, policy, gate set, or assurance does not match evidence"
        )


def ensure_canonical_evidence_file(path: Path, model: _ModelT) -> _ModelT:
    """Create a shared identity file once, or verify the existing exact model."""

    try:
        path.lstat()
    except FileNotFoundError:
        try:
            _write_model(path, model)
            return model
        except SandboxEvidenceError:
            # Another evidence process may have won the exclusive create.
            pass
    except OSError as error:
        raise SandboxEvidenceError(
            "shared evidence identity file is unavailable"
        ) from error
    existing = _load_json_model(path, type(model), require_canonical=True)
    if existing != model:
        raise SandboxEvidenceError("shared evidence identity changed between repeats")
    return existing


class CliSchemaField(_StrictModel):
    """One exact field and JSON value type observed from ``wsb.exe --raw``."""

    name: StrictStr = Field(min_length=1, max_length=128)
    value_type: SchemaValueType

    @field_validator("name")
    @classmethod
    def name_is_safe(cls, value: str) -> str:
        if _CONTROL_CHARACTERS.search(value):
            raise ValueError("CLI schema field contains control characters")
        return value


class CliSchemaEntry(_StrictModel):
    """Bounded raw hashes plus the normalized shape for one CLI stage."""

    stage: SchemaStage
    root_type: SchemaRootType
    fields: tuple[CliSchemaField, ...] = Field(max_length=64)
    observed_statuses: tuple[StrictStr, ...] = Field(max_length=32)
    normalized_shape_sha256: Digest
    raw_response_sha256s: tuple[Digest, ...] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def values_are_canonical(self) -> CliSchemaEntry:
        field_names = tuple(field.name for field in self.fields)
        if tuple(sorted(set(field_names))) != field_names:
            raise ValueError("CLI schema fields must be sorted and unique")
        if self.root_type == "array" and self.fields:
            raise ValueError("array CLI roots cannot declare object fields")
        if tuple(sorted(set(self.observed_statuses))) != self.observed_statuses:
            raise ValueError("CLI statuses must be sorted and unique")
        if any(
            not status or len(status) > 128 or _CONTROL_CHARACTERS.search(status)
            for status in self.observed_statuses
        ):
            raise ValueError("CLI schema contains an unsafe status")
        if tuple(sorted(set(self.raw_response_sha256s))) != self.raw_response_sha256s:
            raise ValueError("raw CLI response hashes must be sorted and unique")
        return self

    def profile_payload(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "root_type": self.root_type,
            "fields": [field.model_dump(mode="json") for field in self.fields],
            "observed_statuses": list(self.observed_statuses),
            "normalized_shape_sha256": self.normalized_shape_sha256,
        }


class CliExecutionIdentity(_StrictModel):
    """One guest execution identity bound to the host CLI transcript."""

    instance_id: CanonicalUuid
    run_id: CanonicalUuid
    request_hash: Digest

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.instance_id, self.run_id, self.request_hash)


class CliSchemaReport(_StrictModel):
    """Mandatory CLI shapes plus the exact execution transcript identity."""

    version: Literal[1] = EVIDENCE_VERSION
    repeat_id: SafeIdentifier
    execution_nonce: ExecutionNonce
    execution_identities: tuple[CliExecutionIdentity, ...] = Field(
        min_length=1,
        max_length=128,
    )
    transcript_sha256: Digest
    entries: tuple[CliSchemaEntry, ...] = Field(
        min_length=len(REQUIRED_CLI_SCHEMA_STAGES),
        max_length=len(REQUIRED_CLI_SCHEMA_STAGES),
    )

    @model_validator(mode="after")
    def values_are_canonical(self) -> CliSchemaReport:
        stages = tuple(entry.stage for entry in self.entries)
        if stages != REQUIRED_CLI_SCHEMA_STAGES:
            raise ValueError(
                "CLI schema report must contain every required stage in fixed order"
            )
        identity_keys = tuple(identity.key for identity in self.execution_identities)
        if identity_keys != tuple(sorted(set(identity_keys))):
            raise ValueError("CLI execution identities must be sorted and unique")
        for label, values in (
            (
                "instance IDs",
                tuple(identity.instance_id for identity in self.execution_identities),
            ),
            (
                "run IDs",
                tuple(identity.run_id for identity in self.execution_identities),
            ),
            (
                "request hashes",
                tuple(identity.request_hash for identity in self.execution_identities),
            ),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"CLI execution {label} must be unique")
        return self

    def profile_payload(self) -> dict[str, object]:
        """Return only stable CLI shape information, never run identities."""

        return {
            "version": self.version,
            "entries": [entry.profile_payload() for entry in self.entries],
        }

    @property
    def profile_sha256(self) -> str:
        return _digest_payload(self.profile_payload())


class RawCliObservation(_StrictModel):
    """One exact, identity-bound host CLI completion."""

    version: Literal[1] = EVIDENCE_VERSION
    repeat_id: SafeIdentifier
    execution_nonce: ExecutionNonce
    sequence: StrictInt = Field(ge=1, le=MAX_RAW_OBSERVATIONS)
    stage: SchemaStage
    argv: tuple[StrictStr, ...] = Field(min_length=3, max_length=16)
    instance_id: CanonicalUuid
    run_id: CanonicalUuid
    request_hash: Digest
    returncode: StrictInt | None
    timed_out: StrictBool
    cancelled: StrictBool
    output_limited: StrictBool
    raw_b64: StrictStr = Field(
        min_length=0,
        max_length=((MAX_RAW_CLI_RESPONSE_BYTES + 2) // 3) * 4,
    )
    raw_sha256: Digest

    @model_validator(mode="after")
    def completion_and_raw_are_bound(self) -> RawCliObservation:
        flags = sum((self.timed_out, self.cancelled, self.output_limited))
        if flags > 1:
            raise ValueError("CLI completion cannot have multiple terminal flags")
        if flags and self.returncode is not None:
            raise ValueError("terminated CLI completion cannot have a return code")
        if not flags and self.returncode is None:
            raise ValueError("completed CLI invocation requires a return code")
        _validate_cli_argv(self.stage, self.argv, self.instance_id)
        try:
            raw = base64.b64decode(
                self.raw_b64.encode("ascii"),
                validate=True,
            )
        except (UnicodeEncodeError, ValueError) as error:
            raise ValueError("raw CLI response is not canonical base64") from error
        if (
            len(raw) > MAX_RAW_CLI_RESPONSE_BYTES
            or base64.b64encode(raw).decode("ascii") != self.raw_b64
        ):
            raise ValueError("raw CLI response size or base64 is invalid")
        if sha256(raw).hexdigest() != self.raw_sha256:
            raise ValueError("raw CLI response digest does not match")
        if not raw and not flags:
            raise ValueError(
                "only a terminated CLI invocation may have empty raw stdout"
            )
        if raw and not flags and self.returncode == 0:
            _validate_successful_cli_observation(self)
        return self

    @classmethod
    def create(
        cls,
        *,
        repeat_id: str,
        execution_nonce: str,
        sequence: int,
        stage: str,
        raw: bytes,
        argv: Sequence[str],
        instance_id: str,
        run_id: str,
        request_hash: str,
        returncode: int | None,
        timed_out: bool,
        cancelled: bool,
        output_limited: bool,
    ) -> RawCliObservation:
        if not isinstance(raw, bytes):
            raise SandboxEvidenceError("raw CLI response must be bytes")
        try:
            return cls(
                repeat_id=repeat_id,
                execution_nonce=execution_nonce,
                sequence=sequence,
                stage=cast(SchemaStage, stage),
                argv=tuple(argv),
                instance_id=instance_id,
                run_id=run_id,
                request_hash=request_hash,
                returncode=returncode,
                timed_out=timed_out,
                cancelled=cancelled,
                output_limited=output_limited,
                raw_b64=base64.b64encode(raw).decode("ascii"),
                raw_sha256=sha256(raw).hexdigest(),
            )
        except ValueError as error:
            raise SandboxEvidenceError("raw CLI observation is invalid") from error

    def canonical_bytes(self) -> bytes:
        return _canonical_model_bytes(self)

    @property
    def identity(self) -> CliExecutionIdentity:
        return CliExecutionIdentity(
            instance_id=self.instance_id,
            run_id=self.run_id,
            request_hash=self.request_hash,
        )

    @property
    def completed_successfully(self) -> bool:
        return (
            self.returncode == 0
            and not self.timed_out
            and not self.cancelled
            and not self.output_limited
        )

    @property
    def raw_bytes(self) -> bytes:
        return base64.b64decode(self.raw_b64.encode("ascii"), validate=True)


class RawObservationRecorder:
    """Append one canonical, run-bound execution transcript."""

    __slots__ = (
        "_closed",
        "_execution_nonce",
        "_path",
        "_repeat_id",
        "_sequence",
        "_stream",
        "_lock",
    )

    def __init__(
        self,
        path: Path,
        *,
        repeat_id: str,
        execution_nonce: str,
    ) -> None:
        if not _is_safe_identifier(repeat_id):
            raise SandboxEvidenceError("raw transcript repeat ID is invalid")
        if not isinstance(execution_nonce, str) or not re.fullmatch(
            r"[0-9a-f]{32}",
            execution_nonce,
        ):
            raise SandboxEvidenceError("raw transcript execution nonce is invalid")
        self._path = path
        self._repeat_id = repeat_id
        self._execution_nonce = execution_nonce
        self._sequence = 0
        self._closed = False
        self._lock = Lock()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = path.open("xb")
        except OSError as error:
            raise SandboxEvidenceError(
                "raw CLI evidence file could not be created exclusively"
            ) from error

    def __enter__(self) -> RawObservationRecorder:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def record(
        self,
        stage: str,
        raw: bytes,
        *,
        argv: Sequence[str],
        instance_id: str,
        run_id: str,
        request_hash: str,
        returncode: int | None,
        timed_out: bool,
        cancelled: bool,
        output_limited: bool,
    ) -> RawCliObservation:
        """Persist one exact CLI completion and flush it before returning."""

        with self._lock:
            if self._closed:
                raise SandboxEvidenceError("raw CLI recorder is closed")
            sequence = self._sequence + 1
            if sequence > MAX_RAW_OBSERVATIONS:
                raise SandboxEvidenceError("too many raw CLI observations")
            observation = RawCliObservation.create(
                repeat_id=self._repeat_id,
                execution_nonce=self._execution_nonce,
                sequence=sequence,
                stage=stage,
                raw=raw,
                argv=argv,
                instance_id=instance_id,
                run_id=run_id,
                request_hash=request_hash,
                returncode=returncode,
                timed_out=timed_out,
                cancelled=cancelled,
                output_limited=output_limited,
            )
            payload = observation.canonical_bytes() + b"\n"
            try:
                if self._stream.tell() + len(payload) > (
                    MAX_RAW_OBSERVATION_JSONL_BYTES
                ):
                    raise SandboxEvidenceError(
                        "raw CLI observation JSONL exceeds its size limit"
                    )
                self._stream.write(payload)
                self._stream.flush()
                os.fsync(self._stream.fileno())
            except OSError as error:
                raise SandboxEvidenceError(
                    "raw CLI observation could not be persisted"
                ) from error
            self._sequence = sequence
            return observation

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._stream.close()
            except OSError as error:
                raise SandboxEvidenceError(
                    "raw CLI observation file could not be closed"
                ) from error
            finally:
                self._closed = True


def build_cli_schema_report(raw_jsonl_path: Path) -> CliSchemaReport:
    """Derive schema and execution identity from one canonical transcript."""

    observations = _load_raw_observations(raw_jsonl_path)
    repeat_ids = {observation.repeat_id for observation in observations}
    nonces = {observation.execution_nonce for observation in observations}
    if len(repeat_ids) != 1 or len(nonces) != 1:
        raise SandboxEvidenceError(
            "raw CLI transcript mixes repeat IDs or execution nonces"
        )
    identities = _validate_execution_transcript(observations)
    entries: list[CliSchemaEntry] = []
    for stage in REQUIRED_CLI_SCHEMA_STAGES:
        matching = tuple(
            observation
            for observation in observations
            if observation.stage == stage
            and observation.completed_successfully
            and observation.raw_bytes
        )
        if not matching:
            raise SandboxEvidenceError(
                "raw CLI transcript lacks a successful non-empty response for "
                f"required stage: {stage}"
            )
        roots: set[SchemaRootType] = set()
        fields: set[tuple[tuple[str, SchemaValueType], ...]] = set()
        statuses: set[str] = set()
        shapes: set[str] = set()
        raw_hashes: set[str] = set()
        for observation in matching:
            raw = observation.raw_bytes
            root_type, observed_fields, observed_statuses, shape_digest = (
                _schema_details(raw, stage=cast(SchemaStage, stage))
            )
            roots.add(root_type)
            fields.add(
                tuple((field.name, field.value_type) for field in observed_fields)
            )
            statuses.update(observed_statuses)
            shapes.add(shape_digest)
            raw_hashes.add(observation.raw_sha256)
        if len(roots) != 1 or len(fields) != 1 or len(shapes) != 1:
            raise SandboxEvidenceError(
                f"raw CLI schema was inconsistent within stage: {stage}"
            )
        field_values = next(iter(fields))
        entries.append(
            CliSchemaEntry(
                stage=cast(SchemaStage, stage),
                root_type=next(iter(roots)),
                fields=tuple(
                    CliSchemaField(name=name, value_type=value_type)
                    for name, value_type in field_values
                ),
                observed_statuses=tuple(sorted(statuses)),
                normalized_shape_sha256=next(iter(shapes)),
                raw_response_sha256s=tuple(sorted(raw_hashes)),
            )
        )
    transcript = b"".join(
        observation.canonical_bytes() + b"\n" for observation in observations
    )
    return CliSchemaReport(
        repeat_id=next(iter(repeat_ids)),
        execution_nonce=next(iter(nonces)),
        execution_identities=identities,
        transcript_sha256=sha256(transcript).hexdigest(),
        entries=tuple(entries),
    )


def _validate_cli_argv(
    stage: SchemaStage,
    argv: tuple[str, ...],
    instance_id: str,
) -> None:
    from .windows_sandbox import (
        WSB_EXPORTER_COMMAND,
        WSB_GUEST_CONTROL,
        WSB_GUEST_EXPORT,
        WSB_RUNNER_COMMAND,
    )

    if any(
        not argument
        or len(argument) > 32_767
        or "\0" in argument
        or _CONTROL_CHARACTERS.search(argument)
        for argument in argv
    ):
        raise ValueError("CLI argv contains an unsafe argument")
    executable = PureWindowsPath(argv[0])
    if not executable.is_absolute() or executable.name.casefold() != "wsb.exe":
        raise ValueError("CLI argv executable must be an absolute wsb.exe")

    if stage == "start":
        valid = (
            len(argv) == 7
            and argv[1:5] == ("start", "--id", instance_id, "--config")
            and argv[5].startswith("<Configuration>")
            and argv[5].endswith("</Configuration>")
            and argv[6] == "--raw"
        )
    elif stage in {"runner", "exporter"}:
        expected_command = (
            WSB_RUNNER_COMMAND if stage == "runner" else WSB_EXPORTER_COMMAND
        )
        valid = argv == (
            argv[0],
            "exec",
            "--id",
            instance_id,
            "--command",
            expected_command,
            "--run-as",
            "System",
            "--working-directory",
            WSB_GUEST_CONTROL,
            "--raw",
        )
    elif stage == "share":
        host_path = PureWindowsPath(argv[5]) if len(argv) == 10 else None
        valid = (
            len(argv) == 10
            and argv[1:5] == ("share", "--id", instance_id, "--host-path")
            and host_path is not None
            and host_path.is_absolute()
            and argv[6:]
            == (
                "--sandbox-path",
                WSB_GUEST_EXPORT,
                "--allow-write",
                "--raw",
            )
        )
    elif stage == "stop":
        valid = argv == (argv[0], "stop", "--id", instance_id, "--raw")
    else:
        valid = argv == (argv[0], "list", "--raw")
    if not valid:
        raise ValueError(f"CLI argv does not match the audited {stage} grammar")


def _validate_successful_cli_observation(
    observation: RawCliObservation,
) -> frozenset[UUID] | None:
    """Reuse the host executor's exact accepted raw-response semantics."""

    from .windows_sandbox import (
        WsbCliCompleted,
        WsbHostExecutionError,
        _validate_cli_completion,
        _validate_list_completion,
    )

    completed = WsbCliCompleted(
        returncode=observation.returncode,
        stdout=observation.raw_bytes,
    )
    try:
        if observation.stage == "list_after_stop":
            return _validate_list_completion(completed)
        _validate_cli_completion(
            completed,
            stage=observation.stage,
            instance_id=UUID(observation.instance_id),
            require_exit_code=observation.stage in {"runner", "exporter"},
        )
    except (ValueError, WsbHostExecutionError) as error:
        raise ValueError(
            "raw CLI response is not accepted by the host executor"
        ) from error
    return None


def _validate_execution_transcript(
    observations: tuple[RawCliObservation, ...],
) -> tuple[CliExecutionIdentity, ...]:
    grouped: dict[tuple[str, str, str], list[RawCliObservation]] = {}
    order: list[tuple[str, str, str]] = []
    active_key: tuple[str, str, str] | None = None
    closed: set[tuple[str, str, str]] = set()
    for observation in observations:
        key = observation.identity.key
        if key != active_key:
            if active_key is not None:
                closed.add(active_key)
            if key in closed:
                raise SandboxEvidenceError(
                    "raw CLI transcript interleaves execution identities"
                )
            order.append(key)
            active_key = key
        grouped.setdefault(key, []).append(observation)

    for key in order:
        execution = grouped[key]
        stages = tuple(observation.stage for observation in execution)
        list_start = next(
            (index for index, stage in enumerate(stages) if stage == "list_after_stop"),
            -1,
        )
        if list_start < 0 or stages[list_start:] != ("list_after_stop",) * (
            len(stages) - list_start
        ):
            raise SandboxEvidenceError(
                "raw CLI execution must finish with contiguous list-after-stop calls"
            )
        prefix = stages[:list_start]
        full_prefix = ("start", "runner", "share", "exporter", "stop")
        cancelled_prefix = ("start", "runner", "stop")
        if prefix == full_prefix:
            if not all(
                observation.completed_successfully and observation.raw_bytes
                for observation in execution[:list_start]
            ):
                raise SandboxEvidenceError(
                    "completed CLI state machine contains an unsuccessful stage"
                )
        elif prefix == cancelled_prefix:
            runner = execution[1]
            if (
                not execution[0].completed_successfully
                or not execution[0].raw_bytes
                or not runner.cancelled
                or runner.returncode is not None
                or not execution[2].completed_successfully
                or not execution[2].raw_bytes
            ):
                raise SandboxEvidenceError(
                    "aborted CLI state machine is not a bound runner cancellation"
                )
        else:
            raise SandboxEvidenceError(
                "raw CLI execution does not follow the audited state machine"
            )
        list_observations = execution[list_start:]
        if not all(
            observation.completed_successfully and observation.raw_bytes
            for observation in list_observations
        ):
            raise SandboxEvidenceError(
                "list-after-stop transcript contains an unsuccessful completion"
            )
        terminal_ids = _validate_successful_cli_observation(list_observations[-1])
        if terminal_ids is None:
            raise SandboxEvidenceError(
                "terminal list-after-stop response was not a list completion"
            )
        if UUID(key[0]) in terminal_ids:
            raise SandboxEvidenceError(
                "terminal list-after-stop response still contains the instance"
            )

    return tuple(
        CliExecutionIdentity(
            instance_id=instance_id,
            run_id=run_id,
            request_hash=request_hash,
        )
        for instance_id, run_id, request_hash in sorted(grouped)
    )


class TestOutcomeSummary(_StrictModel):
    """JUnit-derived outcomes; every non-pass category must be zero to verify."""

    nodeids: tuple[StrictStr, ...] = Field(max_length=128)
    passed: StrictInt = Field(ge=0)
    failed: StrictInt = Field(ge=0)
    errors: StrictInt = Field(ge=0)
    skipped: StrictInt = Field(ge=0)
    xfailed: StrictInt = Field(ge=0)
    xpassed: StrictInt = Field(ge=0)
    junit_sha256: Digest

    @model_validator(mode="after")
    def nodeids_and_counts_are_consistent(self) -> TestOutcomeSummary:
        if tuple(sorted(set(self.nodeids))) != self.nodeids:
            raise ValueError("JUnit node IDs must be sorted and unique")
        if any(not _is_safe_nodeid(nodeid) for nodeid in self.nodeids):
            raise ValueError("JUnit contains an unsafe node ID")
        total = (
            self.passed
            + self.failed
            + self.errors
            + self.skipped
            + self.xfailed
            + self.xpassed
        )
        if total != len(self.nodeids):
            raise ValueError("JUnit outcome counts do not match test cases")
        return self

    @property
    def has_only_passes(self) -> bool:
        return (
            self.failed == 0
            and self.errors == 0
            and self.skipped == 0
            and self.xfailed == 0
            and self.xpassed == 0
            and self.passed == len(self.nodeids)
        )


class SandboxEvidenceRun(_StrictModel):
    """One canonical, self-hashed target-platform repeat."""

    version: Literal[1] = EVIDENCE_VERSION
    repeat_id: SafeIdentifier
    execution_nonce: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")
    producer_id: SafeIdentifier
    workflow_run_id: StrictInt = Field(ge=1)
    workflow_attempt: StrictInt = Field(ge=1)
    pytest_exit_code: Literal[0]
    started_at: datetime
    finished_at: datetime
    required_test_manifest_sha256: Digest
    platform: PlatformFingerprint
    subject: EvidenceSubject
    cli_schema: CliSchemaReport
    schema_profile_sha256: Digest
    tests: TestOutcomeSummary
    evidence_sha256: Digest

    @field_validator("started_at", "finished_at")
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def bindings_and_hash_match(self) -> SandboxEvidenceRun:
        manifest_digest = required_test_manifest().manifest_sha256
        if self.finished_at < self.started_at:
            raise ValueError("evidence finish time precedes its start time")
        if self.required_test_manifest_sha256 != manifest_digest:
            raise ValueError("evidence uses an unrecognized required test manifest")
        if self.subject.required_test_manifest_sha256 != manifest_digest:
            raise ValueError("evidence subject is not bound to the required tests")
        if (
            self.cli_schema.repeat_id != self.repeat_id
            or self.cli_schema.execution_nonce != self.execution_nonce
        ):
            raise ValueError(
                "CLI transcript is not bound to the evidence repeat and nonce"
            )
        if self.schema_profile_sha256 != self.cli_schema.profile_sha256:
            raise ValueError("CLI schema profile digest does not match")
        if self.evidence_sha256 != _digest_payload(self.hash_payload()):
            raise ValueError("evidence run digest does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        repeat_id: str,
        execution_nonce: str,
        producer_id: str,
        workflow_run_id: int,
        workflow_attempt: int,
        pytest_exit_code: int,
        started_at: datetime,
        finished_at: datetime,
        platform: PlatformFingerprint,
        subject: EvidenceSubject,
        cli_schema: CliSchemaReport,
        tests: TestOutcomeSummary,
    ) -> SandboxEvidenceRun:
        payload: dict[str, object] = {
            "version": EVIDENCE_VERSION,
            "repeat_id": repeat_id,
            "execution_nonce": execution_nonce,
            "producer_id": producer_id,
            "workflow_run_id": workflow_run_id,
            "workflow_attempt": workflow_attempt,
            "pytest_exit_code": pytest_exit_code,
            "started_at": started_at,
            "finished_at": finished_at,
            "required_test_manifest_sha256": (required_test_manifest().manifest_sha256),
            "platform": platform,
            "subject": subject,
            "cli_schema": cli_schema,
            "schema_profile_sha256": cli_schema.profile_sha256,
            "tests": tests,
        }
        provisional = _construct_unvalidated(
            cls,
            {
                **payload,
                "evidence_sha256": "0" * 64,
            },
        )
        return cls.model_validate(
            {
                **payload,
                "evidence_sha256": _digest_payload(provisional.hash_payload()),
            }
        )

    def hash_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"evidence_sha256"})

    def canonical_bytes(self) -> bytes:
        return _canonical_model_bytes(self)


class SandboxEvidenceAggregate(_StrictModel):
    """Verified repeated evidence; still not a runtime certification."""

    version: Literal[1] = EVIDENCE_VERSION
    required_test_manifest_sha256: Digest
    platform: PlatformFingerprint
    subject: EvidenceSubject
    schema_profile_sha256: Digest
    evidence_started_at: datetime
    evidence_finished_at: datetime
    repeat_ids: tuple[SafeIdentifier, ...] = Field(
        min_length=MINIMUM_EVIDENCE_REPEATS,
        max_length=64,
    )
    producer_ids: tuple[SafeIdentifier, ...] = Field(min_length=1, max_length=64)
    evidence_run_sha256s: tuple[Digest, ...] = Field(
        min_length=MINIMUM_EVIDENCE_REPEATS,
        max_length=64,
    )
    aggregate_sha256: Digest

    @field_validator("evidence_started_at", "evidence_finished_at")
    @classmethod
    def evidence_timestamps_are_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("aggregate evidence timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def values_and_hash_are_canonical(self) -> SandboxEvidenceAggregate:
        for name, values in (
            ("repeat IDs", self.repeat_ids),
            ("producer IDs", self.producer_ids),
            ("evidence run hashes", self.evidence_run_sha256s),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} must be sorted and unique")
        if self.required_test_manifest_sha256 != (
            required_test_manifest().manifest_sha256
        ):
            raise ValueError("aggregate uses an unrecognized test manifest")
        if self.evidence_finished_at < self.evidence_started_at:
            raise ValueError("aggregate evidence finish precedes its start")
        if self.aggregate_sha256 != _digest_payload(self.hash_payload()):
            raise ValueError("evidence aggregate digest does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        platform: PlatformFingerprint,
        subject: EvidenceSubject,
        schema_profile_sha256: str,
        evidence_started_at: datetime,
        evidence_finished_at: datetime,
        repeat_ids: Sequence[str],
        producer_ids: Sequence[str],
        evidence_run_sha256s: Sequence[str],
    ) -> SandboxEvidenceAggregate:
        payload: dict[str, object] = {
            "version": EVIDENCE_VERSION,
            "required_test_manifest_sha256": (required_test_manifest().manifest_sha256),
            "platform": platform,
            "subject": subject,
            "schema_profile_sha256": schema_profile_sha256,
            "evidence_started_at": evidence_started_at,
            "evidence_finished_at": evidence_finished_at,
            "repeat_ids": tuple(sorted(repeat_ids)),
            "producer_ids": tuple(sorted(producer_ids)),
            "evidence_run_sha256s": tuple(sorted(evidence_run_sha256s)),
        }
        provisional = _construct_unvalidated(
            cls,
            {
                **payload,
                "aggregate_sha256": "0" * 64,
            },
        )
        return cls.model_validate(
            {
                **payload,
                "aggregate_sha256": _digest_payload(provisional.hash_payload()),
            }
        )

    def hash_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"aggregate_sha256"})

    def canonical_bytes(self) -> bytes:
        return _canonical_model_bytes(self)


class IndependentSecurityReview(_StrictModel):
    """A separate review bound to one exact evidence aggregate."""

    version: Literal[1] = CERTIFICATION_VERSION
    review_id: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")
    reviewer_id: SafeIdentifier
    aggregate_sha256: Digest
    disposition: Literal["approved", "rejected"]
    open_findings: StrictInt = Field(ge=0)
    closed_finding_ids: tuple[SafeIdentifier, ...] = Field(max_length=256)
    reviewed_at: datetime
    review_sha256: Digest

    @field_validator("reviewed_at")
    @classmethod
    def reviewed_at_is_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("review timestamp must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def values_and_hash_match(self) -> IndependentSecurityReview:
        if tuple(sorted(set(self.closed_finding_ids))) != self.closed_finding_ids:
            raise ValueError("closed finding IDs must be sorted and unique")
        if self.disposition == "approved" and self.open_findings != 0:
            raise ValueError("approved reviews cannot retain open findings")
        if self.review_sha256 != _digest_payload(self.hash_payload()):
            raise ValueError("independent review digest does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        review_id: str,
        reviewer_id: str,
        aggregate_sha256: str,
        disposition: Literal["approved", "rejected"],
        open_findings: int,
        closed_finding_ids: Sequence[str],
        reviewed_at: datetime,
    ) -> IndependentSecurityReview:
        payload: dict[str, object] = {
            "version": CERTIFICATION_VERSION,
            "review_id": review_id,
            "reviewer_id": reviewer_id,
            "aggregate_sha256": aggregate_sha256,
            "disposition": disposition,
            "open_findings": open_findings,
            "closed_finding_ids": tuple(sorted(closed_finding_ids)),
            "reviewed_at": reviewed_at,
        }
        provisional = _construct_unvalidated(
            cls,
            {
                **payload,
                "review_sha256": "0" * 64,
            },
        )
        return cls.model_validate(
            {
                **payload,
                "review_sha256": _digest_payload(provisional.hash_payload()),
            }
        )

    def hash_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"review_sha256"})

    def canonical_bytes(self) -> bytes:
        return _canonical_model_bytes(self)


@dataclass(frozen=True, slots=True)
class ReviewTrustPins:
    """Explicit out-of-band pins.  The empty default trusts no review."""

    reviewer_ids: frozenset[str] = frozenset()
    review_sha256s: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if any(not _is_safe_identifier(value) for value in self.reviewer_ids):
            raise ValueError("reviewer trust pin is invalid")
        if any(not _is_digest(value) for value in self.review_sha256s):
            raise ValueError("review digest trust pin is invalid")


class SandboxCertification(_StrictModel):
    """Compact certification issued only after pinned independent review."""

    version: Literal[1] = CERTIFICATION_VERSION
    aggregate_sha256: Digest
    platform_sha256: Digest
    subject_sha256: Digest
    schema_profile_sha256: Digest
    evidence_run_sha256s: tuple[Digest, ...] = Field(
        min_length=MINIMUM_EVIDENCE_REPEATS,
        max_length=64,
    )
    review_sha256: Digest
    issued_at: datetime
    expires_at: datetime
    certification_sha256: Digest

    @field_validator("issued_at", "expires_at")
    @classmethod
    def certification_timestamps_are_aware_utc(
        cls,
        value: datetime,
    ) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("certification timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def values_and_hash_match(self) -> SandboxCertification:
        if tuple(sorted(set(self.evidence_run_sha256s))) != self.evidence_run_sha256s:
            raise ValueError("certification evidence hashes must be sorted and unique")
        lifetime = self.expires_at - self.issued_at
        if lifetime <= timedelta(0) or lifetime > MAX_CERTIFICATION_LIFETIME:
            raise ValueError("certification lifetime is outside the allowed range")
        if self.certification_sha256 != _digest_payload(self.hash_payload()):
            raise ValueError("certification digest does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        aggregate: SandboxEvidenceAggregate,
        review: IndependentSecurityReview,
        issued_at: datetime,
        expires_at: datetime,
    ) -> SandboxCertification:
        payload: dict[str, object] = {
            "version": CERTIFICATION_VERSION,
            "aggregate_sha256": aggregate.aggregate_sha256,
            "platform_sha256": _digest_model(aggregate.platform),
            "subject_sha256": _digest_model(aggregate.subject),
            "schema_profile_sha256": aggregate.schema_profile_sha256,
            "evidence_run_sha256s": aggregate.evidence_run_sha256s,
            "review_sha256": review.review_sha256,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }
        provisional = _construct_unvalidated(
            cls,
            {
                **payload,
                "certification_sha256": "0" * 64,
            },
        )
        return cls.model_validate(
            {
                **payload,
                "certification_sha256": _digest_payload(provisional.hash_payload()),
            }
        )

    def hash_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"certification_sha256"})

    def canonical_bytes(self) -> bytes:
        return _canonical_model_bytes(self)


@dataclass(frozen=True, slots=True)
class VerifiedEvidenceBundle:
    """A fully replayed artifact bundle, not merely parsed summary JSON."""

    root: Path
    aggregate: SandboxEvidenceAggregate
    runner_binary_path: Path
    attestation_path: Path
    attestation_sha256: str


AttestationVerifier = Callable[[Path, Path, EvidenceSubject], None]


def collect_evidence_run(
    *,
    repeat_id: str,
    execution_nonce: str,
    producer_id: str,
    workflow_run_id: int,
    workflow_attempt: int,
    pytest_exit_code: int,
    started_at: datetime,
    finished_at: datetime,
    platform: PlatformFingerprint,
    subject: EvidenceSubject,
    cli_schema: CliSchemaReport,
    junit_path: Path,
) -> SandboxEvidenceRun:
    """Collect one self-hashed record without treating failures as success."""

    if pytest_exit_code != 0:
        raise SandboxEvidenceError(
            "pytest exit code must be zero before evidence can be collected"
        )
    tests = _parse_junit(junit_path)
    return SandboxEvidenceRun.create(
        repeat_id=repeat_id,
        execution_nonce=execution_nonce,
        producer_id=producer_id,
        workflow_run_id=workflow_run_id,
        workflow_attempt=workflow_attempt,
        pytest_exit_code=pytest_exit_code,
        started_at=started_at,
        finished_at=finished_at,
        platform=platform,
        subject=subject,
        cli_schema=cli_schema,
        tests=tests,
    )


def verify_evidence_runs(
    runs: Sequence[SandboxEvidenceRun],
) -> SandboxEvidenceAggregate:
    """Verify repeated evidence and return a non-certifying aggregate."""

    if len(runs) < MINIMUM_EVIDENCE_REPEATS:
        raise SandboxEvidenceError(
            f"at least {MINIMUM_EVIDENCE_REPEATS} evidence repeats are required"
        )
    if len(runs) > 64:
        raise SandboxEvidenceError("too many evidence repeats")
    repeat_ids = {run.repeat_id for run in runs}
    nonces = {run.execution_nonce for run in runs}
    run_hashes = {run.evidence_sha256 for run in runs}
    transcript_hashes = {run.cli_schema.transcript_sha256 for run in runs}
    if len(repeat_ids) != len(runs):
        raise SandboxEvidenceError("evidence repeat IDs must be unique")
    if len(nonces) != len(runs):
        raise SandboxEvidenceError("evidence execution nonces must be unique")
    if len(run_hashes) != len(runs):
        raise SandboxEvidenceError("duplicate evidence runs are forbidden")
    if len(transcript_hashes) != len(runs):
        raise SandboxEvidenceError(
            "evidence repeats must have distinct execution transcripts"
        )

    expected_nodeids = REQUIRED_WINDOWS_SANDBOX_TESTS
    first = runs[0]
    seen_instances: set[str] = set()
    seen_run_ids: set[str] = set()
    seen_request_hashes: set[str] = set()
    for run in runs:
        if (
            run.workflow_run_id != first.workflow_run_id
            or run.workflow_attempt != first.workflow_attempt
            or run.producer_id != first.producer_id
        ):
            raise SandboxEvidenceError(
                "evidence repeats must come from one workflow attempt and producer"
            )
        if not run.tests.has_only_passes:
            raise SandboxEvidenceError(
                "every evidence repeat must have zero failures, errors, skips, "
                "xfails, and xpasses"
            )
        if run.tests.nodeids != expected_nodeids:
            raise SandboxEvidenceError(
                "evidence repeat does not contain the fixed required test manifest"
            )
        if run.platform != first.platform:
            raise SandboxEvidenceError(
                "platform fingerprint changed between evidence repeats"
            )
        if run.subject != first.subject:
            raise SandboxEvidenceError("evidence subject changed between repeats")
        if (
            run.schema_profile_sha256 != first.schema_profile_sha256
            or run.cli_schema.profile_payload() != first.cli_schema.profile_payload()
        ):
            raise SandboxEvidenceError(
                "WSB CLI schema changed between evidence repeats"
            )
        instances = {
            identity.instance_id for identity in run.cli_schema.execution_identities
        }
        run_ids = {identity.run_id for identity in run.cli_schema.execution_identities}
        request_hashes = {
            identity.request_hash for identity in run.cli_schema.execution_identities
        }
        if (
            instances & seen_instances
            or run_ids & seen_run_ids
            or request_hashes & seen_request_hashes
        ):
            raise SandboxEvidenceError(
                "evidence repeat execution identity sets must not overlap"
            )
        seen_instances.update(instances)
        seen_run_ids.update(run_ids)
        seen_request_hashes.update(request_hashes)
    ordered_runs = sorted(runs, key=lambda run: (run.started_at, run.repeat_id))
    if any(
        previous.finished_at > current.started_at
        for previous, current in zip(ordered_runs, ordered_runs[1:], strict=False)
    ):
        raise SandboxEvidenceError(
            "evidence repeats must execute serially without overlap"
        )

    return SandboxEvidenceAggregate.create(
        platform=first.platform,
        subject=first.subject,
        schema_profile_sha256=first.schema_profile_sha256,
        evidence_started_at=min(run.started_at for run in runs),
        evidence_finished_at=max(run.finished_at for run in runs),
        repeat_ids=tuple(repeat_ids),
        producer_ids=tuple({run.producer_id for run in runs}),
        evidence_run_sha256s=tuple(run_hashes),
    )


def verify_evidence_bundle(
    bundle_root: Path,
    *,
    attestation_verifier: AttestationVerifier | None = None,
) -> VerifiedEvidenceBundle:
    """Replay every raw artifact and verify the signed aggregate provenance."""

    root = _require_bundle_directory(bundle_root, "evidence bundle root")
    manifest = _load_json_model(
        root / "required-tests.json",
        RequiredTestManifest,
        require_canonical=True,
    )
    if manifest != required_test_manifest():
        raise SandboxEvidenceError("bundle required-test manifest is not current")
    platform = _load_json_model(
        root / "platform.json",
        PlatformFingerprint,
        require_canonical=True,
    )
    subject = _load_json_model(
        root / "subject.json",
        EvidenceSubject,
        require_canonical=True,
    )
    aggregate = _load_json_model(
        root / "aggregate.json",
        SandboxEvidenceAggregate,
        require_canonical=True,
    )
    if aggregate.repeat_ids != EXPECTED_EVIDENCE_REPEAT_IDS:
        raise SandboxEvidenceError("bundle must contain the fixed three repeat IDs")

    artifact_paths = _validate_evidence_bundle_tree(root, aggregate.repeat_ids)
    runner_source = artifact_paths["runner_source"]
    runner_binary = artifact_paths["runner_binary"]
    probe_binary = artifact_paths["probe_binary"]
    wheel = artifact_paths["wheel"]
    for path, expected, label in (
        (runner_source, subject.runner_source_sha256, "runner source"),
        (runner_binary, subject.runner_binary_sha256, "runner binary"),
        (probe_binary, subject.probe_binary_sha256, "security probe binary"),
        (wheel, subject.wheel_sha256, "wheel"),
    ):
        if _hash_identity_file(path, label=f"bundle {label}") != expected:
            raise SandboxEvidenceError(f"bundle {label} digest does not match subject")

    runs: list[SandboxEvidenceRun] = []
    for repeat_id in aggregate.repeat_ids:
        repeat_root = root / repeat_id
        raw_path = repeat_root / "cli-raw.jsonl"
        schema = _load_json_model(
            repeat_root / "cli-schema.json",
            CliSchemaReport,
            require_canonical=True,
        )
        rebuilt_schema = build_cli_schema_report(raw_path)
        if rebuilt_schema != schema:
            raise SandboxEvidenceError(
                f"{repeat_id} CLI schema does not replay from its raw transcript"
            )
        run = _load_json_model(
            repeat_root / "evidence-run.json",
            SandboxEvidenceRun,
            require_canonical=True,
        )
        tests = _parse_junit(repeat_root / "junit.xml")
        rebuilt_run = SandboxEvidenceRun.create(
            repeat_id=run.repeat_id,
            execution_nonce=run.execution_nonce,
            producer_id=run.producer_id,
            workflow_run_id=run.workflow_run_id,
            workflow_attempt=run.workflow_attempt,
            pytest_exit_code=run.pytest_exit_code,
            started_at=run.started_at,
            finished_at=run.finished_at,
            platform=platform,
            subject=subject,
            cli_schema=rebuilt_schema,
            tests=tests,
        )
        if rebuilt_run != run:
            raise SandboxEvidenceError(
                f"{repeat_id} evidence does not replay from raw CLI and JUnit"
            )
        runs.append(run)

    rebuilt_aggregate = verify_evidence_runs(tuple(runs))
    if rebuilt_aggregate != aggregate:
        raise SandboxEvidenceError(
            "bundle aggregate does not replay from its three evidence runs"
        )
    if aggregate.platform != platform or aggregate.subject != subject:
        raise SandboxEvidenceError("bundle identity files do not match the aggregate")

    attestation_path = root / "aggregate.attestation.sigstore.json"
    attestation_sha256 = _hash_identity_file(
        attestation_path,
        label="aggregate provenance attestation",
    )
    verifier = (
        verify_github_attestation
        if attestation_verifier is None
        else (attestation_verifier)
    )
    verifier(root / "aggregate.json", attestation_path, subject)
    return VerifiedEvidenceBundle(
        root=root,
        aggregate=aggregate,
        runner_binary_path=runner_binary,
        attestation_path=attestation_path,
        attestation_sha256=attestation_sha256,
    )


def verify_github_attestation(
    aggregate_path: Path,
    attestation_path: Path,
    subject: EvidenceSubject,
) -> None:
    """Use GitHub CLI's Sigstore verifier with a fixed repository identity."""

    located = shutil.which("gh")
    if not located:
        raise SandboxEvidenceError(
            "GitHub CLI is required for attestation verification"
        )
    gh = Path(located)
    _hash_identity_file(gh, label="GitHub CLI", allow_hardlinks=True)
    environment = {
        name: value
        for name in ("SystemRoot", "WINDIR", "TEMP", "TMP")
        if (value := os.environ.get(name))
    }
    environment["PATH"] = str(gh.parent)
    environment["NO_COLOR"] = "1"
    try:
        completed = subprocess.run(
            (
                str(gh),
                "attestation",
                "verify",
                str(aggregate_path),
                "--repo",
                ATTESTED_GITHUB_REPOSITORY,
                "--bundle",
                str(attestation_path),
                "--signer-workflow",
                ATTESTED_GITHUB_WORKFLOW,
                "--source-digest",
                subject.git_commit_sha,
                "--source-ref",
                ATTESTED_GITHUB_REF,
                "--format",
                "json",
            ),
            shell=False,
            cwd=aggregate_path.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SandboxEvidenceError(
            "GitHub provenance attestation verification failed closed"
        ) from error
    if (
        completed.returncode != 0
        or not completed.stdout
        or len(completed.stdout) > MAX_EVIDENCE_JSON_BYTES
        or len(completed.stderr) > MAX_EVIDENCE_JSON_BYTES
    ):
        raise SandboxEvidenceError("GitHub provenance attestation did not verify")
    verified = _strict_json_value(completed.stdout)
    if not isinstance(verified, list) or not verified:
        raise SandboxEvidenceError(
            "GitHub provenance verifier returned no verified attestation"
        )


def issue_certification(
    aggregate: SandboxEvidenceAggregate,
    review: IndependentSecurityReview,
    *,
    trust_pins: ReviewTrustPins = ReviewTrustPins(),
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> SandboxCertification:
    """Issue only for a pinned, independent, closed review of certified code."""

    _verify_review(aggregate, review, trust_pins)
    if aggregate.subject.security_assurance != CERTIFIED_SECURITY_ASSURANCE:
        raise SandboxEvidenceError("candidate security assurance cannot be certified")
    issued = issued_at or datetime.now(timezone.utc)
    evidence_deadline = aggregate.evidence_finished_at + MAX_EVIDENCE_VALIDITY
    expires = expires_at or min(
        issued + MAX_CERTIFICATION_LIFETIME,
        evidence_deadline,
    )
    _require_issue_after_review(review, issued)
    certification = SandboxCertification.create(
        aggregate=aggregate,
        review=review,
        issued_at=issued,
        expires_at=expires,
    )
    _require_certification_evidence_window(aggregate, certification)
    return certification


def verify_certification(
    certification: SandboxCertification,
    aggregate: SandboxEvidenceAggregate,
    review: IndependentSecurityReview,
    *,
    trust_pins: ReviewTrustPins = ReviewTrustPins(),
    now: datetime | None = None,
) -> None:
    """Validate an existing certification; empty pins always reject it."""

    _verify_review(aggregate, review, trust_pins)
    if aggregate.subject.security_assurance != CERTIFIED_SECURITY_ASSURANCE:
        raise SandboxEvidenceError(
            "candidate security assurance cannot satisfy certification"
        )
    _require_issue_after_review(review, certification.issued_at)
    expected = SandboxCertification.create(
        aggregate=aggregate,
        review=review,
        issued_at=certification.issued_at,
        expires_at=certification.expires_at,
    )
    if expected != certification:
        raise SandboxEvidenceError(
            "certification is not bound to this aggregate and review"
        )
    _require_certification_evidence_window(aggregate, certification)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise SandboxEvidenceError("certification verification time must be aware")
    if current < certification.issued_at or current >= certification.expires_at:
        raise SandboxEvidenceError("certification is not currently valid")


def _verify_review(
    aggregate: SandboxEvidenceAggregate,
    review: IndependentSecurityReview,
    trust_pins: ReviewTrustPins,
) -> None:
    if not trust_pins.reviewer_ids or not trust_pins.review_sha256s:
        raise SandboxEvidenceError("no independent review trust pins are configured")
    if review.reviewer_id not in trust_pins.reviewer_ids:
        raise SandboxEvidenceError("independent reviewer is not trusted")
    if review.review_sha256 not in trust_pins.review_sha256s:
        raise SandboxEvidenceError("independent review digest is not trusted")
    if review.reviewer_id in aggregate.producer_ids:
        raise SandboxEvidenceError(
            "evidence producer cannot independently approve its own evidence"
        )
    if review.aggregate_sha256 != aggregate.aggregate_sha256:
        raise SandboxEvidenceError("review is not bound to this evidence aggregate")
    if review.reviewed_at < aggregate.evidence_finished_at:
        raise SandboxEvidenceError(
            "independent review cannot predate completed evidence"
        )
    if (
        review.reviewed_at - aggregate.evidence_finished_at
        > MAX_EVIDENCE_TO_REVIEW_DELAY
    ):
        raise SandboxEvidenceError(
            "independent review exceeded the evidence freshness window"
        )
    if review.disposition != "approved" or review.open_findings != 0:
        raise SandboxEvidenceError(
            "independent review is not approved with zero open findings"
        )
    if review.closed_finding_ids != REQUIRED_SECURITY_GATE_IDS:
        raise SandboxEvidenceError(
            "independent review did not close the fixed security gate set"
        )


def _require_issue_after_review(
    review: IndependentSecurityReview,
    issued_at: datetime,
) -> None:
    if issued_at.tzinfo is None or issued_at.utcoffset() is None:
        raise SandboxEvidenceError("certification issue time must be aware")
    if issued_at.astimezone(timezone.utc) < review.reviewed_at:
        raise SandboxEvidenceError(
            "certification cannot be issued before the independent review"
        )


def _require_certification_evidence_window(
    aggregate: SandboxEvidenceAggregate,
    certification: SandboxCertification,
) -> None:
    evidence_deadline = aggregate.evidence_finished_at + MAX_EVIDENCE_VALIDITY
    if certification.issued_at >= evidence_deadline:
        raise SandboxEvidenceError(
            "certification was issued after the evidence validity window"
        )
    if certification.expires_at > evidence_deadline:
        raise SandboxEvidenceError(
            "certification outlives the evidence validity window"
        )


def _parse_junit(path: Path) -> TestOutcomeSummary:
    raw = _read_bounded_regular_file(path, MAX_JUNIT_XML_BYTES)
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise SandboxEvidenceError("JUnit DTD and entities are forbidden")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as error:
        raise SandboxEvidenceError("JUnit XML is invalid") from error
    cases = _validate_junit_suite_counters(root)
    if len(cases) > 128:
        raise SandboxEvidenceError("JUnit contains too many test cases")

    outcomes = {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
    }
    nodeids: list[str] = []
    for case in cases:
        nodeids.append(_junit_case_nodeid(case))
        outcome = _junit_case_outcome(case)
        outcomes[outcome] += 1
    sorted_nodeids = tuple(sorted(nodeids))
    if len(set(sorted_nodeids)) != len(sorted_nodeids):
        raise SandboxEvidenceError("JUnit contains duplicate test cases")
    return TestOutcomeSummary(
        nodeids=sorted_nodeids,
        passed=outcomes["passed"],
        failed=outcomes["failed"],
        errors=outcomes["errors"],
        skipped=outcomes["skipped"],
        xfailed=outcomes["xfailed"],
        xpassed=outcomes["xpassed"],
        junit_sha256=sha256(raw).hexdigest(),
    )


def _validate_junit_suite_counters(
    root: ElementTree.Element,
) -> tuple[ElementTree.Element, ...]:
    if root.tag != "testsuites":
        raise SandboxEvidenceError("JUnit root must be one pytest testsuites element")
    root_children = tuple(root)
    if len(root_children) != 1 or root_children[0].tag != "testsuite":
        raise SandboxEvidenceError(
            "JUnit must contain exactly one direct pytest testsuite"
        )
    suite = root_children[0]
    suite_children = tuple(suite)
    if any(child.tag != "testcase" for child in suite_children):
        raise SandboxEvidenceError(
            "JUnit testsuite may contain only direct testcase elements"
        )
    cases = cast(tuple[ElementTree.Element, ...], suite_children)
    for case in cases:
        _validate_junit_case_structure(case)
    expected = {
        "tests": len(cases),
        "failures": sum(case.find("failure") is not None for case in cases),
        "errors": sum(case.find("error") is not None for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
    }
    for name, count in expected.items():
        value = suite.attrib.get(name)
        if value is None or not re.fullmatch(r"0|[1-9][0-9]*", value):
            raise SandboxEvidenceError(
                f"JUnit testsuite has an invalid or missing {name} counter"
            )
        if int(value) != count:
            raise SandboxEvidenceError(
                f"JUnit testsuite {name} counter does not match testcases"
            )
    return cases


def _validate_junit_case_structure(case: ElementTree.Element) -> None:
    allowed_children = frozenset(
        {"failure", "error", "skipped", "system-out", "system-err"}
    )
    children = tuple(case)
    if any(child.tag not in allowed_children for child in children):
        raise SandboxEvidenceError(
            "JUnit testcase contains an unsupported or nested child"
        )
    child_tags = tuple(child.tag for child in children)
    if len(child_tags) != len(set(child_tags)):
        raise SandboxEvidenceError("JUnit testcase contains duplicate child elements")
    if sum(tag in {"failure", "error", "skipped"} for tag in child_tags) > 1:
        raise SandboxEvidenceError("JUnit testcase contains contradictory outcomes")


def _junit_case_nodeid(case: ElementTree.Element) -> str:
    name = case.attrib.get("name", "")
    classname = case.attrib.get("classname", "")
    if (
        not name
        or not classname
        or _CONTROL_CHARACTERS.search(name)
        or _CONTROL_CHARACTERS.search(classname)
    ):
        raise SandboxEvidenceError("JUnit testcase has an invalid name or classname")
    candidates = [
        nodeid
        for nodeid in REQUIRED_WINDOWS_SANDBOX_TESTS
        if nodeid.rsplit("::", 1)[-1] == name and classname == _nodeid_classname(nodeid)
    ]
    if len(candidates) != 1:
        raise SandboxEvidenceError(
            "JUnit testcase cannot be mapped uniquely to the required manifest"
        )
    return candidates[0]


def _junit_case_outcome(case: ElementTree.Element) -> str:
    error = case.find("error")
    failure = case.find("failure")
    skipped = case.find("skipped")
    marker_text = " ".join(
        (
            *(str(value) for value in case.attrib.values()),
            *(
                str(value)
                for element in case.iter()
                for value in element.attrib.values()
            ),
            *(element.text or "" for element in case.iter()),
        )
    ).casefold()
    if error is not None:
        return "errors"
    if "xpass" in marker_text:
        return "xpassed"
    if failure is not None:
        return "failed"
    if skipped is not None and "xfail" in marker_text:
        return "xfailed"
    if skipped is not None:
        return "skipped"
    return "passed"


def _nodeid_module_name(nodeid: str) -> str:
    path = nodeid.split("::", 1)[0]
    if path.endswith(".py"):
        path = path[:-3]
    return path.replace("/", ".")


def _nodeid_classname(nodeid: str) -> str:
    parts = nodeid.split("::")
    class_parts = parts[1:-1]
    return ".".join((_nodeid_module_name(nodeid), *class_parts))


def _is_safe_nodeid(value: str) -> bool:
    return (
        1 <= len(value) <= 512
        and value.startswith("tests/")
        and "::" in value
        and "\\" not in value
        and "\0" not in value
        and _CONTROL_CHARACTERS.search(value) is None
        and ".." not in Path(value.split("::", 1)[0]).parts
    )


def _is_safe_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}", value)
    )


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _construct_unvalidated(
    model_type: type[_ModelT],
    values: dict[str, object],
) -> _ModelT:
    """Construct only the temporary object used to derive a self-hash."""

    constructor = cast(Any, model_type).model_construct
    return cast(_ModelT, constructor(**values))


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SandboxEvidenceError("evidence is not canonical JSON") from error


def _canonical_model_bytes(model: BaseModel) -> bytes:
    return _canonical_json_bytes(model.model_dump(mode="json"))


def _digest_payload(value: object) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _digest_model(model: BaseModel) -> str:
    return sha256(_canonical_model_bytes(model)).hexdigest()


def _require_absolute_path(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise SandboxEvidenceError(f"{label} must be an absolute Path")
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise SandboxEvidenceError(f"{label} is unavailable") from error


def _require_bundle_directory(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise SandboxEvidenceError(f"{label} must be an absolute Path")
    try:
        original = path.lstat()
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as error:
        raise SandboxEvidenceError(f"{label} is unavailable") from error
    reparse_attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    if (
        resolved != path
        or path.is_symlink()
        or resolved.is_symlink()
        or not stat.S_ISDIR(original.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or int(getattr(original, "st_file_attributes", 0)) & reparse_attribute
        or int(getattr(metadata, "st_file_attributes", 0)) & reparse_attribute
    ):
        raise SandboxEvidenceError(f"{label} must be a real non-reparse directory")
    return resolved


def _validate_evidence_bundle_tree(
    root: Path,
    repeat_ids: Sequence[str],
) -> dict[str, Path]:
    expected_root_names = {
        "aggregate.attestation.sigstore.json",
        "aggregate.json",
        "build",
        "platform.json",
        "required-tests.json",
        "subject.json",
        *repeat_ids,
    }
    optional_root_names = {"certification.json", "independent-review.json"}
    try:
        root_entries = {entry.name: entry for entry in root.iterdir()}
    except OSError as error:
        raise SandboxEvidenceError(
            "evidence bundle root cannot be enumerated"
        ) from error
    if not expected_root_names <= set(root_entries) or not set(root_entries) <= (
        expected_root_names | optional_root_names
    ):
        raise SandboxEvidenceError(
            "evidence bundle root has missing or unknown entries"
        )
    for optional in optional_root_names & set(root_entries):
        _hash_identity_file(root_entries[optional], label=f"bundle {optional}")

    build = _require_bundle_directory(root / "build", "bundle build directory")
    build_entries = {entry.name: entry for entry in build.iterdir()}
    if set(build_entries) != {"guest-runner", "security-probe", "wheel"}:
        raise SandboxEvidenceError("bundle build directory has an unknown shape")
    guest_runner = _require_bundle_directory(
        build / "guest-runner",
        "bundle guest-runner directory",
    )
    security_probe = _require_bundle_directory(
        build / "security-probe",
        "bundle security-probe directory",
    )
    wheel_root = _require_bundle_directory(build / "wheel", "bundle wheel directory")
    guest_entries = {entry.name: entry for entry in guest_runner.iterdir()}
    if set(guest_entries) != {
        "neil-sandbox-runner.exe",
        "sandbox_guest_runner.cs",
    }:
        raise SandboxEvidenceError("bundle guest runner has an unknown shape")
    probe_entries = {entry.name: entry for entry in security_probe.iterdir()}
    if set(probe_entries) != {"sandbox-security-probe.exe"}:
        raise SandboxEvidenceError("bundle security probe has an unknown shape")
    wheel_entries = tuple(wheel_root.iterdir())
    if len(wheel_entries) != 1 or not wheel_entries[0].name.lower().endswith(".whl"):
        raise SandboxEvidenceError("bundle must contain exactly one wheel")

    for repeat_id in repeat_ids:
        repeat_root = _require_bundle_directory(
            root / repeat_id,
            f"bundle {repeat_id} directory",
        )
        repeat_entries = {entry.name: entry for entry in repeat_root.iterdir()}
        if set(repeat_entries) != {
            "cli-raw.jsonl",
            "cli-schema.json",
            "evidence-run.json",
            "junit.xml",
        }:
            raise SandboxEvidenceError(f"bundle {repeat_id} has an unknown shape")

    return {
        "runner_source": guest_runner / "sandbox_guest_runner.cs",
        "runner_binary": guest_runner / "neil-sandbox-runner.exe",
        "probe_binary": security_probe / "sandbox-security-probe.exe",
        "wheel": wheel_entries[0],
    }


def _hash_identity_file(
    path: Path,
    *,
    label: str,
    allow_hardlinks: bool = False,
) -> str:
    """Hash one bounded stable regular file without following reparse points."""

    try:
        original = path.lstat()
        resolved = _require_absolute_path(path, label)
        metadata = resolved.lstat()
    except OSError as error:
        raise SandboxEvidenceError(f"{label} is unavailable") from error
    original_attributes = int(getattr(original, "st_file_attributes", 0))
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    if (
        path.is_symlink()
        or original_attributes & reparse_attribute
        or not stat.S_ISREG(metadata.st_mode)
        or resolved.is_symlink()
        or attributes & reparse_attribute
        or (not allow_hardlinks and metadata.st_nlink != 1)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_IDENTITY_FILE_BYTES
    ):
        raise SandboxEvidenceError(f"{label} is not a safe bounded file")
    digest = sha256()
    total = 0
    try:
        with resolved.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_IDENTITY_FILE_BYTES:
                    raise SandboxEvidenceError(
                        f"{label} exceeds its evidence size limit"
                    )
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise SandboxEvidenceError(f"{label} could not be read") from error
    if total != metadata.st_size or not _same_file(metadata, after):
        raise SandboxEvidenceError(f"{label} changed while being hashed")
    return digest.hexdigest()


def _read_bounded_regular_file(path: Path, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SandboxEvidenceError("evidence input is unavailable") from error
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or attributes & reparse_attribute
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > maximum
    ):
        raise SandboxEvidenceError("evidence input is not a safe bounded file")
    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise SandboxEvidenceError("evidence input could not be read") from error
    if (
        len(raw) > maximum
        or len(raw) != metadata.st_size
        or not _same_file(metadata, after)
    ):
        raise SandboxEvidenceError("evidence input changed while being read")
    return raw


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        # Windows path stat infers execute bits from a .exe suffix while
        # fstat(handle) has no filename; compare the kernel file type instead.
        and stat.S_IFMT(before.st_mode) == stat.S_IFMT(after.st_mode)
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
    )


def _load_raw_observations(path: Path) -> tuple[RawCliObservation, ...]:
    raw = _read_bounded_regular_file(path, MAX_RAW_OBSERVATION_JSONL_BYTES)
    if not raw.endswith(b"\n"):
        raise SandboxEvidenceError("raw CLI observation JSONL is truncated")
    lines = raw.splitlines()
    if not lines or len(lines) > MAX_RAW_OBSERVATIONS:
        raise SandboxEvidenceError("raw CLI observation count is invalid")
    observations: list[RawCliObservation] = []
    for expected_sequence, line in enumerate(lines, start=1):
        if not line:
            raise SandboxEvidenceError(
                "raw CLI observation JSONL contains a blank line"
            )
        _strict_json_value(line)
        try:
            observation = RawCliObservation.model_validate_json(line, strict=True)
        except ValueError as error:
            raise SandboxEvidenceError(
                "raw CLI observation does not match its schema"
            ) from error
        if observation.canonical_bytes() != line:
            raise SandboxEvidenceError("raw CLI observation is not canonical")
        if observation.sequence != expected_sequence:
            raise SandboxEvidenceError(
                "raw CLI observation sequences must be contiguous"
            )
        observations.append(observation)
    return tuple(observations)


def _schema_details(
    raw: bytes,
    *,
    stage: SchemaStage | None = None,
) -> tuple[
    SchemaRootType,
    tuple[CliSchemaField, ...],
    tuple[str, ...],
    str,
]:
    if not raw or len(raw) > MAX_RAW_CLI_RESPONSE_BYTES:
        raise SandboxEvidenceError("raw CLI response size is invalid")
    value = _strict_json_value(raw)
    return _schema_details_from_value(value, stage=stage)


def _schema_details_from_value(
    value: object,
    *,
    stage: SchemaStage | None = None,
) -> tuple[
    SchemaRootType,
    tuple[CliSchemaField, ...],
    tuple[str, ...],
    str,
]:
    if isinstance(value, dict):
        root_type: SchemaRootType = "object"
        fields = tuple(
            CliSchemaField(
                name=name,
                value_type=_json_value_type(item),
            )
            for name, item in sorted(value.items())
        )
        statuses = tuple(
            sorted(
                {
                    item
                    for name in ("Status", "State")
                    if isinstance((item := value.get(name)), str)
                }
            )
        )
    elif isinstance(value, list):
        root_type = "array"
        fields = ()
        statuses = ()
    else:
        raise SandboxEvidenceError("raw CLI JSON root must be an object or array")
    return (
        root_type,
        fields,
        statuses,
        _digest_payload(
            _normalized_list_response_shape(value)
            if stage == "list_after_stop"
            else _normalized_json_shape(value)
        ),
    )


def _normalized_list_response_shape(value: object) -> object:
    """Describe the executor's list grammar without encoding list contents."""

    item_contract = {
        "type": "object",
        "required_fields": ({"name": "Id", "type": "string"},),
    }
    collection_contract = {
        "type": "array",
        "items": (item_contract,),
    }
    if isinstance(value, list):
        return collection_contract
    if isinstance(value, dict):
        return {
            "type": "object",
            "fields": (
                {
                    "name": "Status",
                    "shape": {"type": "string"},
                },
                {
                    "name": "Success",
                    "shape": {"type": "boolean"},
                },
                {
                    "name": "WindowsSandboxEnvironments",
                    "shape": collection_contract,
                },
            ),
        }
    raise SandboxEvidenceError(
        "list-after-stop response must be an audited object or array"
    )


def _json_value_type(value: object) -> SchemaValueType:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    raise SandboxEvidenceError("raw CLI JSON contains an unsupported value type")


def _normalized_json_shape(value: object) -> object:
    items = 0

    def walk(current: object, depth: int) -> object:
        nonlocal items
        items += 1
        if items > 4_096 or depth > 16:
            raise SandboxEvidenceError("raw CLI JSON shape exceeds its limits")
        value_type = _json_value_type(current)
        if isinstance(current, dict):
            return {
                "type": value_type,
                "fields": [
                    {
                        "name": name,
                        "shape": walk(item, depth + 1),
                    }
                    for name, item in sorted(current.items())
                ],
            }
        if isinstance(current, list):
            shapes = {_canonical_json_bytes(walk(item, depth + 1)) for item in current}
            return {
                "type": value_type,
                "items": [
                    json.loads(shape.decode("utf-8")) for shape in sorted(shapes)
                ],
            }
        return {"type": value_type}

    return walk(value, 0)


def _strict_json_value(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8", errors="strict")

        def reject_duplicates(
            pairs: list[tuple[str, object]],
        ) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        return json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_float=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON float: {value}")
            ),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise SandboxEvidenceError("evidence input is not strict JSON") from error


def _load_json_model(
    path: Path,
    model_type: type[_ModelT],
    *,
    require_canonical: bool,
) -> _ModelT:
    raw = _read_bounded_regular_file(path, MAX_EVIDENCE_JSON_BYTES)
    _strict_json_value(raw)
    try:
        model = model_type.model_validate_json(raw, strict=True)
    except ValueError as error:
        raise SandboxEvidenceError("evidence JSON does not match its schema") from error
    if require_canonical and _canonical_model_bytes(model) != raw:
        raise SandboxEvidenceError("evidence JSON is not canonical")
    return model


def _write_model(path: Path, model: BaseModel) -> None:
    payload = _canonical_model_bytes(model)
    if len(payload) > MAX_EVIDENCE_JSON_BYTES:
        raise SandboxEvidenceError("canonical evidence exceeds its size limit")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise SandboxEvidenceError(
            "canonical evidence could not be written exclusively"
        ) from error


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise SandboxEvidenceError("timestamp is not valid ISO 8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SandboxEvidenceError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m neil_agent.sandbox_evidence",
        description="Collect and verify strict Windows Sandbox evidence.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--output", required=True, type=Path)

    schema = subparsers.add_parser("schema")
    schema.add_argument("--raw-jsonl", required=True, type=Path)
    schema.add_argument("--output", required=True, type=Path)

    collect = subparsers.add_parser("collect")
    collect.add_argument("--repeat-id", required=True)
    collect.add_argument("--execution-nonce", required=True)
    collect.add_argument("--producer-id", required=True)
    collect.add_argument("--workflow-run-id", required=True, type=int)
    collect.add_argument("--workflow-attempt", required=True, type=int)
    collect.add_argument("--pytest-exit-code", required=True, type=int)
    collect.add_argument("--started-at", required=True)
    collect.add_argument("--finished-at", required=True)
    collect.add_argument("--platform", required=True, type=Path)
    collect.add_argument("--subject", required=True, type=Path)
    collect.add_argument("--schema", required=True, type=Path)
    collect.add_argument("--junit", required=True, type=Path)
    collect.add_argument("--output", required=True, type=Path)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--run", required=True, action="append", type=Path)
    verify.add_argument("--output", required=True, type=Path)

    bundle_verify = subparsers.add_parser("bundle-verify")
    bundle_verify.add_argument("--bundle-root", required=True, type=Path)

    review_command = subparsers.add_parser("review")
    review_command.add_argument("--bundle-root", required=True, type=Path)
    review_command.add_argument("--review-id", required=True)
    review_command.add_argument("--reviewer-id", required=True)
    review_command.add_argument("--reviewed-at", required=True)
    review_command.add_argument("--output", required=True, type=Path)

    certify = subparsers.add_parser("certify")
    certify.add_argument("--bundle-root", required=True, type=Path)
    certify.add_argument("--review", required=True, type=Path)
    certify.add_argument("--trusted-reviewer", action="append", default=[])
    certify.add_argument("--trusted-review-sha256", action="append", default=[])
    certify.add_argument("--issued-at", required=True)
    certify.add_argument("--expires-at", required=True)
    certify.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Workflow-oriented CLI.  Returns two for every fail-closed rejection."""

    parser = _build_argument_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "manifest":
            _write_model(arguments.output, required_test_manifest())
        elif arguments.command == "schema":
            _write_model(
                arguments.output,
                build_cli_schema_report(arguments.raw_jsonl),
            )
        elif arguments.command == "collect":
            platform = _load_json_model(
                arguments.platform,
                PlatformFingerprint,
                require_canonical=False,
            )
            subject = _load_json_model(
                arguments.subject,
                EvidenceSubject,
                require_canonical=False,
            )
            schema = _load_json_model(
                arguments.schema,
                CliSchemaReport,
                require_canonical=False,
            )
            run = collect_evidence_run(
                repeat_id=arguments.repeat_id,
                execution_nonce=arguments.execution_nonce,
                producer_id=arguments.producer_id,
                workflow_run_id=arguments.workflow_run_id,
                workflow_attempt=arguments.workflow_attempt,
                pytest_exit_code=arguments.pytest_exit_code,
                started_at=_parse_timestamp(arguments.started_at),
                finished_at=_parse_timestamp(arguments.finished_at),
                platform=platform,
                subject=subject,
                cli_schema=schema,
                junit_path=arguments.junit,
            )
            _write_model(arguments.output, run)
        elif arguments.command == "verify":
            runs = tuple(
                _load_json_model(
                    path,
                    SandboxEvidenceRun,
                    require_canonical=True,
                )
                for path in arguments.run
            )
            _write_model(arguments.output, verify_evidence_runs(runs))
        elif arguments.command == "bundle-verify":
            verify_evidence_bundle(arguments.bundle_root)
        elif arguments.command == "review":
            bundle = verify_evidence_bundle(arguments.bundle_root)
            review = IndependentSecurityReview.create(
                review_id=arguments.review_id,
                reviewer_id=arguments.reviewer_id,
                aggregate_sha256=bundle.aggregate.aggregate_sha256,
                disposition="approved",
                open_findings=0,
                closed_finding_ids=REQUIRED_SECURITY_GATE_IDS,
                reviewed_at=_parse_timestamp(arguments.reviewed_at),
            )
            _write_model(arguments.output, review)
        elif arguments.command == "certify":
            aggregate = verify_evidence_bundle(arguments.bundle_root).aggregate
            review = _load_json_model(
                arguments.review,
                IndependentSecurityReview,
                require_canonical=True,
            )
            pins = ReviewTrustPins(
                reviewer_ids=frozenset(arguments.trusted_reviewer),
                review_sha256s=frozenset(arguments.trusted_review_sha256),
            )
            certification = issue_certification(
                aggregate,
                review,
                trust_pins=pins,
                issued_at=_parse_timestamp(arguments.issued_at),
                expires_at=_parse_timestamp(arguments.expires_at),
            )
            _write_model(arguments.output, certification)
        else:  # pragma: no cover - argparse owns the command choices.
            raise SandboxEvidenceError("unknown evidence command")
    except (SandboxEvidenceError, ValueError) as error:
        print(f"sandbox evidence rejected: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
