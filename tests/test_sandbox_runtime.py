"""Runtime certification and conditional sandbox-tool integration tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from neil_agent.errors import SandboxError
from neil_agent.sandbox import (
    RunSpec,
    SandboxCertification,
    SandboxPolicy,
    SandboxResult,
    WindowsSandboxBackend,
)
from neil_agent.sandbox_runtime import (
    SandboxRuntimeCertificationUnavailable,
    VerifiedRuntimeCertification,
    load_runtime_certification,
)
from neil_agent.sandbox_snapshot import prepare_snapshot
from neil_agent.schemas import ToolCall
from neil_agent.tools.registry import ToolRegistry
from neil_agent.tools.sandbox import SandboxCommandTools
from neil_agent.windows_sandbox import WsbExecutionPlan, WsbExecutionResult


def _certification(runner_binary: bytes) -> SandboxCertification:
    return SandboxCertification(
        backend="windows-sandbox",
        git_commit_sha="1" * 40,
        evidence_sha256="2" * 64,
        provenance_sha256="a" * 64,
        independent_review_sha256="3" * 64,
        certification_sha256="b" * 64,
        executable_sha256="4" * 64,
        runner_source_sha256="5" * 64,
        runner_binary_sha256=sha256(runner_binary).hexdigest(),
        policy_version=1,
        protocol_version=2,
        required_gate_ids=(
            "actions-provenance",
            "network-denial",
            "result-integrity",
        ),
    )


@dataclass(frozen=True)
class _Material:
    certification: SandboxCertification
    runner_binary_path: Path
    expires_at: datetime


class _Executor:
    def __init__(self) -> None:
        self.plans: list[WsbExecutionPlan] = []

    def execute(
        self,
        plan: WsbExecutionPlan,
        *,
        cancel: object | None = None,
    ) -> WsbExecutionResult:
        del cancel
        self.plans.append(plan)
        return WsbExecutionResult(
            instance_id=plan.instance_id,
            run_id=plan.run_id,
            request_hash=plan.request_hash,
            status="exited",
            exit_code=0,
            stdout=b"ok",
            stderr=b"",
            duration_ms=12,
            error_code=None,
            result_hash="6" * 64,
            job_terminated=True,
        )


def _backend(tmp_path: Path) -> tuple[WindowsSandboxBackend, _Executor]:
    wsb = tmp_path / "wsb.exe"
    wsb.write_bytes(b"wsb")
    runner_payload = b"certified-runner"
    runner = tmp_path / "neil-sandbox-runner.exe"
    runner.write_bytes(runner_payload)
    material = _Material(
        certification=_certification(runner_payload),
        runner_binary_path=runner.resolve(),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    executor = _Executor()
    backend = WindowsSandboxBackend(
        platform_name="nt",
        executable_locator=lambda name: str(wsb) if name == "wsb.exe" else None,
        runtime_loader=lambda _: material,
        host_executor_factory=lambda _: executor,
    )
    return backend, executor


def test_missing_runtime_bundle_never_creates_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("SANDBOX_CERTIFICATION_ROOT", raising=False)

    with pytest.raises(SandboxRuntimeCertificationUnavailable):
        load_runtime_certification((tmp_path / "wsb.exe").resolve())


def test_certified_backend_maps_bounded_guest_result(tmp_path: Path) -> None:
    backend, executor = _backend(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "tool.exe").write_bytes(b"tool")

    with prepare_snapshot(source.resolve(), (tmp_path / "snapshot").resolve()) as snap:
        result = backend.run(
            RunSpec(
                executable=snap.root / "tool.exe",
                argv=("--version",),
                policy=SandboxPolicy(workspace_mode="read-only-snapshot"),
                workspace_snapshot=snap.root,
            )
        )

    assert backend.probe().ready is True
    assert result.termination_reason == "succeeded"
    assert result.stdout == "ok"
    assert len(executor.plans) == 1
    assert executor.plans[0].certification_sha256 == "b" * 64


def test_run_command_is_registered_only_when_ready_and_discards_changes(
    tmp_path: Path,
) -> None:
    backend, _ = _backend(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "tool.exe").write_bytes(b"tool")
    registry = ToolRegistry()
    tools = SandboxCommandTools(workspace, backend)

    assert tools.register_if_ready(registry) is True
    call = ToolCall(
        id="call-1",
        name="run_command",
        arguments={"executable": "tool.exe", "argv": ["--version"]},
    )
    preview = registry.preview(call)
    executed = registry.execute(
        call,
        approved=True,
        approved_preview=preview.content,
    )

    assert preview.is_error is False
    assert executed.is_error is False
    assert json.loads(executed.content)["guest_modifications"] == "discarded"


def test_run_command_stages_declared_guest_export_for_import(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from neil_agent.tools.filesystem import FileSystemTools
    from neil_agent.tools.guest_import import GuestExportImportTools

    backend, _ = _backend(tmp_path)
    stub_backend = MagicMock(wraps=backend)
    stub_backend.run.return_value = SandboxResult(
        backend="windows-sandbox",
        termination_reason="succeeded",
        exit_code=0,
        stdout="ok",
        run_id="c" * 32,
        request_hash="d" * 64,
        certification_sha256="b" * 64,
        exported_files=(("out/result.txt", b"exported\n"),),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "tool.exe").write_bytes(b"tool")
    guest_import = GuestExportImportTools(FileSystemTools(workspace))
    registry = ToolRegistry()
    tools = SandboxCommandTools(workspace, stub_backend, guest_import=guest_import)
    assert tools.register_if_ready(registry) is True
    call = ToolCall(
        id="call-export",
        name="run_command",
        arguments={
            "executable": "tool.exe",
            "argv": ["--write"],
            "export_paths": ["out/result.txt"],
        },
    )
    preview = registry.preview(call)
    executed = registry.execute(
        call,
        approved=True,
        approved_preview=preview.content,
    )
    payload = json.loads(executed.content)
    assert payload["guest_modifications"] == "exported-for-import"
    digest = payload["guest_export"]["staged_import_manifest_sha256"]
    assert guest_import._require_staged(digest)[1]["out/result.txt"] == b"exported\n"


def test_backend_rejects_certification_rotation_after_approval(tmp_path: Path) -> None:
    wsb = tmp_path / "wsb.exe"
    wsb.write_bytes(b"wsb")
    runner = tmp_path / "neil-sandbox-runner.exe"
    runner.write_bytes(b"certified-runner")
    original = _Material(
        certification=_certification(b"certified-runner"),
        runner_binary_path=runner.resolve(),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    current = [original]
    executor = _Executor()
    backend = WindowsSandboxBackend(
        platform_name="nt",
        executable_locator=lambda name: str(wsb) if name == "wsb.exe" else None,
        runtime_loader=lambda _: current[0],
        host_executor_factory=lambda _: executor,
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "tool.exe").write_bytes(b"tool")

    with prepare_snapshot(source.resolve(), (tmp_path / "snapshot").resolve()) as snap:
        spec = RunSpec(
            executable=snap.root / "tool.exe",
            argv=("--version",),
            policy=SandboxPolicy(workspace_mode="read-only-snapshot"),
            workspace_snapshot=snap.root,
        )
        approved_preview = backend.preview(spec)
        current[0] = replace(
            original,
            certification=replace(
                original.certification,
                certification_sha256="c" * 64,
            ),
        )

        with pytest.raises(SandboxError, match="批准后发生变化"):
            backend.run(spec, approved_preview=approved_preview)

    assert executor.plans == []


def test_unready_backend_does_not_register_run_command(tmp_path: Path) -> None:
    wsb = tmp_path / "wsb.exe"
    wsb.write_bytes(b"wsb")
    backend = WindowsSandboxBackend(
        platform_name="nt",
        executable_locator=lambda name: str(wsb) if name == "wsb.exe" else None,
        runtime_loader=lambda _: (_ for _ in ()).throw(
            SandboxRuntimeCertificationUnavailable("missing")
        ),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()

    assert SandboxCommandTools(workspace, backend).register_if_ready(registry) is False
    assert all(definition.name != "run_command" for definition in registry.definitions)


def test_verified_runtime_material_is_frozen(tmp_path: Path) -> None:
    runner = tmp_path / "runner.exe"
    runner.write_bytes(b"runner")
    material = VerifiedRuntimeCertification(
        certification=_certification(b"runner"),
        runner_binary_path=runner.resolve(),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )

    assert material.runner_binary_path == runner.resolve()
