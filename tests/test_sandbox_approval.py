"""Tests for stable, complete Windows Sandbox approval identities."""

from hashlib import sha256
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from neil_agent.approval import ApprovalStore
from neil_agent.sandbox_approval import RunCommandApprovalBinding
from neil_agent.sandbox_guest import SandboxGuestRequest
from neil_agent.schemas import ToolCall


def _binding(**updates: object) -> RunCommandApprovalBinding:
    payload: dict[str, object] = {
        "executable": r"tools\probe.exe",
        "argv": ("--mode", "safe value"),
        "logical_cwd": "tools",
        "snapshot_manifest_sha256": "1" * 64,
        "runner_source_sha256": "2" * 64,
        "runner_binary_sha256": "3" * 64,
        "timeout_ms": 30_000,
        "max_output_bytes": 128_000,
        "active_process_limit": 4,
        "process_memory_bytes": 64 * 1024 * 1024,
        "job_memory_bytes": 128 * 1024 * 1024,
    }
    payload.update(updates)
    return RunCommandApprovalBinding.model_validate(payload)


def _call() -> ToolCall:
    return ToolCall(
        id="call-run",
        name="run_command",
        arguments={
            "executable": r"tools\probe.exe",
            "argv": ["--mode", "safe value"],
        },
    )


def test_binding_has_deterministic_domain_separated_digest() -> None:
    binding = _binding()
    restored = RunCommandApprovalBinding.model_validate_json(
        json.dumps(binding.model_dump(mode="json"), sort_keys=False)
    )

    assert restored.canonical_bytes() == binding.canonical_bytes()
    assert restored.digest == binding.digest
    assert (
        binding.digest
        == sha256(
            b"neil-agent:sandbox-run-command-approval:v1\0" + binding.canonical_bytes()
        ).hexdigest()
    )
    assert binding.approval_binding.model_dump() == {
        "kind": "sandbox-run-command",
        "version": 1,
        "sha256": binding.digest,
    }


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("executable", r"tools\other.exe"),
        ("argv", ("--mode", "different")),
        ("logical_cwd", "."),
        ("snapshot_manifest_sha256", "4" * 64),
        ("runner_source_sha256", "5" * 64),
        ("runner_binary_sha256", "6" * 64),
        ("timeout_ms", 31_000),
        ("max_output_bytes", 129_000),
        ("active_process_limit", 5),
        ("process_memory_bytes", 65 * 1024 * 1024),
        ("job_memory_bytes", 129 * 1024 * 1024),
    ),
)
def test_every_variable_execution_field_changes_digest(
    field: str,
    changed: object,
) -> None:
    original = _binding()
    payload = original.model_dump(mode="python")
    payload[field] = changed
    modified = RunCommandApprovalBinding.model_validate(payload)

    assert modified.digest != original.digest
    assert modified.render_preview() != original.render_preview()


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("version", 2),
        ("backend", "host-process"),
        ("backend_version", 2),
        ("guest_protocol_version", 3),
        ("runner_version", 3),
        ("network_policy", "allow"),
        ("workspace_policy", "read-write"),
        ("environment_policy", "inherit"),
        ("modification_policy", "write-back"),
    ),
)
def test_fixed_security_fields_cannot_be_relaxed(
    field: str,
    changed: object,
) -> None:
    payload = _binding().model_dump(mode="python")
    payload[field] = changed

    with pytest.raises(ValidationError):
        RunCommandApprovalBinding.model_validate(payload)


def test_preview_displays_every_bound_field_without_shell_ambiguity() -> None:
    binding = _binding(argv=("--name", "含 空格"))
    preview = binding.render_preview()

    expected_fragments = (
        'Executable (snapshot-relative): "tools\\\\probe.exe"',
        'Argv (canonical JSON): ["--name","含 空格"]',
        'Logical cwd: "tools"',
        f"Snapshot manifest SHA-256: {binding.snapshot_manifest_sha256}",
        f"Runner source SHA-256: {binding.runner_source_sha256}",
        f"Runner binary SHA-256: {binding.runner_binary_sha256}",
        "Approval binding version: 1",
        "Backend: windows-sandbox",
        "Backend version: 1",
        "Guest protocol version: 2",
        "Runner version: 2",
        "Network policy: deny",
        "Workspace policy: read-only-snapshot",
        "Environment policy: fixed-empty",
        "Modification policy: discard-all",
        "Timeout (ms): 30000",
        "Maximum output (bytes): 128000",
        "Active process limit: 4",
        f"Process memory limit (bytes): {64 * 1024 * 1024}",
        f"Job memory limit (bytes): {128 * 1024 * 1024}",
        f"Approval-Binding-SHA256: {binding.digest}",
    )
    for fragment in expected_fragments:
        assert fragment in preview


def test_binding_reuses_guest_path_argument_and_limit_validation() -> None:
    invalid_payloads = (
        {"executable": r"..\probe.exe"},
        {"executable": "tools/probe.exe"},
        {"executable": r"tools\probe.cmd"},
        {"argv": ("safe", "bad\nargument")},
        {"logical_cwd": r"..\outside"},
        {"active_process_limit": 0},
        {
            "process_memory_bytes": 128 * 1024 * 1024,
            "job_memory_bytes": 64 * 1024 * 1024,
        },
    )

    for update in invalid_payloads:
        with pytest.raises(ValidationError):
            _binding(**update)


def test_approval_store_matches_the_exact_sandbox_binding(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    binding = _binding()
    preview = binding.render_preview()
    request = store.create(
        _call(),
        preview,
        prompt="run the probe",
        instructions="root rules",
        binding=binding.approval_binding,
    )

    assert request.binding_kind == "sandbox-run-command"
    assert request.binding_version == 1
    assert request.binding_sha256 == binding.digest
    assert store.matches(
        request,
        _call(),
        preview,
        prompt="run the probe",
        instructions="root rules",
        binding=binding.approval_binding,
    )
    assert not store.matches(
        request,
        _call(),
        _binding(timeout_ms=31_000).render_preview(),
        prompt="run the probe",
        instructions="root rules",
        binding=_binding(timeout_ms=31_000).approval_binding,
    )


def test_ephemeral_guest_ids_do_not_change_stable_approval_digest() -> None:
    binding = _binding()
    first = SandboxGuestRequest.create(
        run_id="a" * 32,
        instance_id="b" * 32,
        snapshot_manifest_sha256=binding.snapshot_manifest_sha256,
        runner_source_sha256=binding.runner_source_sha256,
        approval_binding_sha256=binding.digest,
        executable=binding.executable,
        argv=binding.argv,
        cwd=binding.logical_cwd,
        timeout_ms=binding.timeout_ms,
        max_output_bytes=binding.max_output_bytes,
        active_process_limit=binding.active_process_limit,
        process_memory_bytes=binding.process_memory_bytes,
        job_memory_bytes=binding.job_memory_bytes,
    )
    second = SandboxGuestRequest.create(
        run_id="c" * 32,
        instance_id="d" * 32,
        snapshot_manifest_sha256=binding.snapshot_manifest_sha256,
        runner_source_sha256=binding.runner_source_sha256,
        approval_binding_sha256=binding.digest,
        executable=binding.executable,
        argv=binding.argv,
        cwd=binding.logical_cwd,
        timeout_ms=binding.timeout_ms,
        max_output_bytes=binding.max_output_bytes,
        active_process_limit=binding.active_process_limit,
        process_memory_bytes=binding.process_memory_bytes,
        job_memory_bytes=binding.job_memory_bytes,
    )

    assert first.request_hash != second.request_hash
    assert _binding().digest == binding.digest
