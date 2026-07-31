"""Tests for fail-closed, platform-neutral sandbox contracts."""

from __future__ import annotations

import os
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Event
from xml.etree import ElementTree

import pytest

from neil_agent import sandbox as sandbox_module
from neil_agent.errors import SandboxError
from neil_agent.sandbox import (
    MAX_OUTPUT_BYTES,
    MAX_SNAPSHOT_FILE_BYTES,
    MAX_SNAPSHOT_TOTAL_BYTES,
    WINDOWS_SANDBOX_GUEST_WORKSPACE,
    RunSpec,
    SandboxCapabilities,
    SandboxCertification,
    SandboxLimits,
    SandboxPolicy,
    SandboxResult,
    WindowsSandboxBackend,
    _WindowsFileInformation,
    _windows_file_link_count,
)


def _absolute_executable(tmp_path: Path) -> Path:
    return (tmp_path / "tool.exe").resolve()


def _certification(
    *,
    backend: str = "windows-sandbox",
) -> SandboxCertification:
    return SandboxCertification(
        backend=backend,
        git_commit_sha="1" * 40,
        evidence_sha256="2" * 64,
        independent_review_sha256="3" * 64,
        executable_sha256="4" * 64,
        runner_source_sha256="5" * 64,
        runner_binary_sha256="6" * 64,
        policy_version=1,
        protocol_version=1,
        required_gate_ids=("network-deny", "process-tree", "result-integrity"),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", 0),
        ("timeout_seconds", float("inf")),
        ("max_output_bytes", 100),
        ("max_memory_bytes", 1_000),
        ("max_processes", 0),
        ("max_processes", True),
    ],
)
def test_sandbox_limits_reject_invalid_or_unbounded_values(
    field: str,
    value: object,
) -> None:
    arguments = {field: value}

    with pytest.raises(ValueError):
        SandboxLimits(**arguments)  # type: ignore[arg-type]


def test_policy_is_immutable_and_canonicalizes_allowlisted_environment() -> None:
    policy = SandboxPolicy(
        environment=(("no_color", "1"), ("LANG", "zh_CN.UTF-8")),
    )

    assert policy.environment == (("LANG", "zh_CN.UTF-8"), ("NO_COLOR", "1"))
    with pytest.raises(FrozenInstanceError):
        policy.network = "allow"  # type: ignore[misc]


@pytest.mark.parametrize(
    "environment",
    [
        (("PATH", r"C:\tools"),),
        (("DEEPSEEK_API_KEY", "secret"),),
        (("GITHUB_TOKEN", "secret"),),
        (("LANG", "safe"), ("lang", "duplicate")),
        (("LANG", "bad\0value"),),
        (("LANG", r"C:\Users\Neil"),),
        (("TZ", "../../secret"),),
    ],
)
def test_policy_rejects_secret_or_unapproved_environment(
    environment: tuple[tuple[str, str], ...],
) -> None:
    with pytest.raises(ValueError):
        SandboxPolicy(environment=environment)


def test_run_spec_requires_absolute_shell_free_executable_and_tuple_argv(
    tmp_path: Path,
) -> None:
    executable = _absolute_executable(tmp_path)
    spec = RunSpec(executable=executable, argv=("--check", "value"))

    assert spec.executable == executable
    assert spec.argv == ("--check", "value")

    with pytest.raises(ValueError, match="absolute"):
        RunSpec(executable=Path("tool.exe"))
    with pytest.raises(ValueError, match="immutable tuple"):
        RunSpec(executable=executable, argv=["--check"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="shell or interpreter"):
        RunSpec(executable=(tmp_path / "run.cmd").resolve())
    with pytest.raises(ValueError, match="argument"):
        RunSpec(executable=executable, argv=("bad\nargument",))


def test_run_spec_binds_snapshot_and_working_directory(
    tmp_path: Path,
) -> None:
    executable = _absolute_executable(tmp_path)
    snapshot = (tmp_path / "snapshot").resolve()
    snapshot.mkdir()

    with pytest.raises(ValueError, match="forbidden"):
        RunSpec(executable=executable, workspace_snapshot=snapshot)
    with pytest.raises(ValueError, match="requires an absolute"):
        RunSpec(
            executable=executable,
            policy=SandboxPolicy(workspace_mode="read-only-snapshot"),
            workspace_snapshot=Path("snapshot"),
        )
    with pytest.raises(ValueError, match="stay inside"):
        RunSpec(
            executable=executable,
            policy=SandboxPolicy(workspace_mode="read-only-snapshot"),
            workspace_snapshot=snapshot,
            working_directory="../outside",
        )

    spec = RunSpec(
        executable=executable,
        policy=SandboxPolicy(workspace_mode="read-only-snapshot"),
        workspace_snapshot=snapshot,
        working_directory=r"src\package",
    )
    assert spec.working_directory == "src/package"


def test_result_has_explicit_timeout_and_cancel_semantics() -> None:
    cancelled = SandboxResult(
        backend="test",
        termination_reason="cancelled",
        exit_code=None,
        elapsed_seconds=0.25,
    )

    assert cancelled.termination_reason == "cancelled"
    with pytest.raises(ValueError, match="cannot report an exit code"):
        SandboxResult(
            backend="test",
            termination_reason="timed_out",
            exit_code=1,
        )
    with pytest.raises(ValueError, match="exit code zero"):
        SandboxResult(
            backend="test",
            termination_reason="succeeded",
            exit_code=None,
        )
    with pytest.raises(ValueError, match="output boundary"):
        SandboxResult(
            backend="test",
            termination_reason="succeeded",
            exit_code=0,
            stdout="x" * (MAX_OUTPUT_BYTES + 1),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend", ""),
        ("git_commit_sha", "1" * 39),
        ("evidence_sha256", "A" * 64),
        ("independent_review_sha256", "2" * 63),
        ("executable_sha256", "not-a-digest"),
        ("runner_source_sha256", "3" * 65),
        ("runner_binary_sha256", ""),
        ("policy_version", 0),
        ("protocol_version", True),
        ("required_gate_ids", ()),
        ("required_gate_ids", ("duplicate", "duplicate")),
        ("required_gate_ids", ("not canonical",)),
        ("required_gate_ids", ("z-last", "a-first")),
    ],
)
def test_sandbox_certification_rejects_unbound_or_noncanonical_evidence(
    field: str,
    value: object,
) -> None:
    arguments = {
        "backend": "windows-sandbox",
        "git_commit_sha": "1" * 40,
        "evidence_sha256": "2" * 64,
        "independent_review_sha256": "3" * 64,
        "executable_sha256": "4" * 64,
        "runner_source_sha256": "5" * 64,
        "runner_binary_sha256": "6" * 64,
        "policy_version": 1,
        "protocol_version": 1,
        "required_gate_ids": ("network-deny",),
    }
    arguments[field] = value

    with pytest.raises(ValueError):
        SandboxCertification(**arguments)  # type: ignore[arg-type]


def test_capability_readiness_is_derived_from_certification_and_all_gates(
    tmp_path: Path,
) -> None:
    executable = (tmp_path / "wsb.exe").resolve()
    certified = SandboxCapabilities(
        backend="windows-sandbox",
        available=True,
        reason_code="ready",
        summary="全部安全门禁可用。",
        executable=executable,
        certification=_certification(),
        workspace_modes=("read-only-snapshot",),
        network_modes=("deny",),
        supports_cancellation=True,
        supports_timeout=True,
        supports_output_limit=True,
        supports_memory_limit=True,
        supports_process_limit=True,
    )
    incomplete = SandboxCapabilities(
        backend="windows-sandbox",
        available=True,
        reason_code="capability_incomplete",
        summary="缺少完整进程限制。",
        executable=executable,
        certification=_certification(),
        workspace_modes=("read-only-snapshot",),
        network_modes=("deny",),
        supports_cancellation=True,
        supports_timeout=True,
        supports_output_limit=True,
        supports_memory_limit=True,
        supports_process_limit=False,
    )

    assert certified.ready is True
    assert certified.capability_gates_complete is True
    assert incomplete.ready is False
    assert incomplete.capability_gates_complete is False
    assert "ready" not in SandboxCapabilities.__dataclass_fields__
    with pytest.raises(TypeError, match="ready"):
        SandboxCapabilities(  # type: ignore[call-arg]
            backend="windows-sandbox",
            available=True,
            ready=True,
            reason_code="ready",
            summary="不能手工设置 ready。",
        )


def test_capabilities_reject_ready_reason_or_certification_contradictions(
    tmp_path: Path,
) -> None:
    executable = (tmp_path / "wsb.exe").resolve()
    complete = {
        "backend": "windows-sandbox",
        "available": True,
        "summary": "严格状态。",
        "executable": executable,
        "workspace_modes": ("read-only-snapshot",),
        "network_modes": ("deny",),
        "supports_cancellation": True,
        "supports_timeout": True,
        "supports_output_limit": True,
        "supports_memory_limit": True,
        "supports_process_limit": True,
    }

    with pytest.raises(ValueError, match="ready reason"):
        SandboxCapabilities(reason_code="ready", **complete)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ready reason code"):
        SandboxCapabilities(
            reason_code="certified_but_misreported",
            certification=_certification(),
            **complete,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="backend does not match"):
        SandboxCapabilities(
            reason_code="capability_incomplete",
            certification=_certification(backend="other-backend"),
            supports_process_limit=False,
            **{
                key: value
                for key, value in complete.items()
                if key != "supports_process_limit"
            },  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="cannot be certified"):
        SandboxCapabilities(
            backend="windows-sandbox",
            available=False,
            reason_code="unsupported_platform",
            summary="不可用。",
            certification=_certification(),
        )
    with pytest.raises(ValueError, match="wsb.exe CLI"):
        SandboxCapabilities(
            backend="windows-sandbox",
            available=True,
            reason_code="certification_required",
            summary="GUI 不能成为执行候选。",
            executable=(tmp_path / "WindowsSandbox.exe").resolve(),
        )


def test_probe_reports_unsupported_or_missing_backend_without_side_effects() -> None:
    calls: list[str] = []

    def missing(name: str) -> None:
        calls.append(name)
        return None

    unsupported = WindowsSandboxBackend(
        platform_name="posix",
        executable_locator=missing,
    ).probe()
    missing_windows = WindowsSandboxBackend(
        platform_name="nt",
        executable_locator=missing,
    ).probe()

    assert unsupported.available is False
    assert unsupported.ready is False
    assert unsupported.reason_code == "unsupported_platform"
    assert calls == ["WindowsSandbox.exe", "wsb.exe"]
    assert missing_windows.available is False
    assert missing_windows.reason_code == "executable_not_found"
    assert missing_windows.executable is None


def test_probe_reports_gui_as_available_but_not_an_execution_candidate(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "WindowsSandbox.exe"
    executable.write_bytes(b"not executed")
    backend = WindowsSandboxBackend(
        platform_name="nt",
        executable_locator=lambda name: (
            str(executable) if name == executable.name else None
        ),
    )

    capabilities = backend.probe()

    assert capabilities.available is True
    assert capabilities.ready is False
    assert capabilities.reason_code == "cli_executable_required"
    assert capabilities.executable is None
    assert capabilities.certification is None
    assert capabilities.workspace_modes == ("none", "read-only-snapshot")
    assert capabilities.network_modes == ("deny",)
    assert capabilities.supports_cancellation is False


def test_probe_exposes_only_wsb_cli_as_uncertified_execution_candidate(
    tmp_path: Path,
) -> None:
    gui = tmp_path / "WindowsSandbox.exe"
    cli = tmp_path / "wsb.exe"
    gui.write_bytes(b"not executed")
    cli.write_bytes(b"not executed")
    backend = WindowsSandboxBackend(
        platform_name="nt",
        executable_locator=lambda name: str(tmp_path / name),
    )

    capabilities = backend.probe()

    assert capabilities.available is True
    assert capabilities.ready is False
    assert capabilities.reason_code == "certification_required"
    assert capabilities.executable == cli.resolve()
    assert capabilities.executable != gui.resolve()
    assert capabilities.certification is None


def test_unavailable_run_fails_closed_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("host process fallback is forbidden"),
    )
    backend = WindowsSandboxBackend(
        platform_name="nt",
        executable_locator=lambda _: None,
    )
    spec = RunSpec(executable=_absolute_executable(tmp_path))

    with pytest.raises(SandboxError, match="不会退化"):
        backend.run(spec, cancel=Event())


def test_detected_but_incomplete_backend_also_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sandbox_executable = tmp_path / "wsb.exe"
    sandbox_executable.write_bytes(b"not executed")
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("Windows Sandbox must not launch"),
    )
    backend = WindowsSandboxBackend(
        platform_name="nt",
        executable_locator=lambda name: (
            str(sandbox_executable) if name == "wsb.exe" else None
        ),
    )

    with pytest.raises(SandboxError, match="执行通道尚未完成"):
        backend.run(RunSpec(executable=_absolute_executable(tmp_path)))


def test_wsb_xml_disables_sharing_and_maps_only_read_only_snapshot(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot & source"
    working_directory = snapshot / "src"
    working_directory.mkdir(parents=True)
    (working_directory / "app.py").write_text("value = 1\n", encoding="utf-8")
    spec = RunSpec(
        executable=_absolute_executable(tmp_path),
        argv=("--secret-looking-argument",),
        policy=SandboxPolicy(
            workspace_mode="read-only-snapshot",
            environment=(("LANG", "PRIVATE-NOT-IN-XML"),),
        ),
        workspace_snapshot=snapshot.resolve(),
        working_directory="src",
    )

    xml = WindowsSandboxBackend(
        platform_name="nt",
        executable_locator=lambda _: None,
    ).build_config_xml(spec)
    document = ElementTree.fromstring(xml)

    expected_settings = {
        "VGpu": "Disable",
        "Networking": "Disable",
        "AudioInput": "Disable",
        "VideoInput": "Disable",
        "PrinterRedirection": "Disable",
        "ClipboardRedirection": "Disable",
        "ProtectedClient": "Enable",
        "MemoryInMB": "2048",
    }
    assert {
        name: document.findtext(name) for name in expected_settings
    } == expected_settings
    mapped = document.find("MappedFolders/MappedFolder")
    assert mapped is not None
    assert mapped.findtext("HostFolder") == str(snapshot.resolve())
    assert mapped.findtext("SandboxFolder") == WINDOWS_SANDBOX_GUEST_WORKSPACE
    assert mapped.findtext("ReadOnly") == "true"
    assert document.find("LogonCommand") is None
    assert "PRIVATE-NOT-IN-XML" not in xml
    assert "--secret-looking-argument" not in xml


def test_wsb_xml_none_mode_maps_no_host_folder(tmp_path: Path) -> None:
    spec = RunSpec(executable=_absolute_executable(tmp_path))

    xml = WindowsSandboxBackend(
        platform_name="nt",
        executable_locator=lambda _: None,
    ).build_config_xml(spec)

    document = ElementTree.fromstring(xml)
    assert document.find("MappedFolders") is None
    assert document.findtext("Networking") == "Disable"


def test_wsb_xml_rejects_memory_limit_windows_sandbox_cannot_enforce(
    tmp_path: Path,
) -> None:
    spec = RunSpec(
        executable=_absolute_executable(tmp_path),
        limits=SandboxLimits(max_memory_bytes=512 * 1024 * 1024),
    )

    with pytest.raises(SandboxError, match="低于 2048 MiB"):
        WindowsSandboxBackend(
            platform_name="nt",
            executable_locator=lambda _: None,
        ).build_config_xml(spec)


@pytest.mark.parametrize(
    "sensitive_name",
    [".env", "private.pem", ".git", ".ssh", "credentials.json"],
)
def test_wsb_xml_rejects_sensitive_snapshot_content(
    tmp_path: Path,
    sensitive_name: str,
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    sensitive = snapshot / sensitive_name
    if sensitive_name in {".git", ".ssh"}:
        sensitive.mkdir()
    else:
        sensitive.write_text("secret", encoding="utf-8")
    spec = RunSpec(
        executable=_absolute_executable(tmp_path),
        policy=SandboxPolicy(workspace_mode="read-only-snapshot"),
        workspace_snapshot=snapshot.resolve(),
    )

    with pytest.raises(SandboxError, match="敏感"):
        WindowsSandboxBackend(
            platform_name="nt",
            executable_locator=lambda _: None,
        ).build_config_xml(spec)


def test_wsb_xml_rejects_snapshot_symlink(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    outside = tmp_path / "outside"
    snapshot.mkdir()
    outside.mkdir()
    link = snapshot / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating directory symlinks is unavailable")
    spec = RunSpec(
        executable=_absolute_executable(tmp_path),
        policy=SandboxPolicy(workspace_mode="read-only-snapshot"),
        workspace_snapshot=snapshot.resolve(),
    )

    with pytest.raises(SandboxError, match="重解析点"):
        WindowsSandboxBackend(
            platform_name="nt",
            executable_locator=lambda _: None,
        ).build_config_xml(spec)


def test_wsb_xml_rejects_snapshot_hard_link(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    outside = tmp_path / "outside.txt"
    snapshot.mkdir()
    outside.write_text("shared", encoding="utf-8")
    linked = snapshot / "linked.txt"
    try:
        os.link(outside, linked)
    except OSError:
        pytest.skip("creating hard links is unavailable")
    spec = RunSpec(
        executable=_absolute_executable(tmp_path),
        policy=SandboxPolicy(workspace_mode="read-only-snapshot"),
        workspace_snapshot=snapshot.resolve(),
    )

    with pytest.raises(SandboxError, match="硬链接"):
        WindowsSandboxBackend(
            platform_name="nt",
            executable_locator=lambda _: None,
        ).build_config_xml(spec)


def test_windows_link_query_failure_still_closes_handle(tmp_path: Path) -> None:
    handle = object()

    class FailingQueryApi:
        def __init__(self) -> None:
            self.closed: list[object] = []

        def open_metadata(self, path: Path) -> object:
            assert path == tmp_path / "file.txt"
            return handle

        def query(self, received: object) -> _WindowsFileInformation:
            assert received is handle
            raise OSError("query failed")

        def close(self, received: object) -> None:
            self.closed.append(received)

    api = FailingQueryApi()

    with pytest.raises(SandboxError, match="无法查询"):
        _windows_file_link_count(tmp_path / "file.txt", api=api)

    assert api.closed == [handle]


def test_windows_link_close_failure_is_fail_closed(tmp_path: Path) -> None:
    handle = object()

    class FailingCloseApi:
        def open_metadata(self, path: Path) -> object:
            assert path == tmp_path / "file.txt"
            return handle

        def query(self, received: object) -> _WindowsFileInformation:
            assert received is handle
            return _WindowsFileInformation(file_attributes=0, link_count=1)

        def close(self, received: object) -> None:
            assert received is handle
            raise OSError("close failed")

    with pytest.raises(SandboxError, match="可靠关闭"):
        _windows_file_link_count(tmp_path / "file.txt", api=FailingCloseApi())


def test_windows_link_open_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FailingOpenApi:
        def open_metadata(self, path: Path) -> object:
            raise OSError("open failed")

        def query(self, handle: object) -> _WindowsFileInformation:
            pytest.fail("query must not run after open failure")

        def close(self, handle: object) -> None:
            pytest.fail("close cannot run without a handle")

    monkeypatch.setattr(
        sandbox_module,
        "_CtypesWindowsFileApi",
        lambda: FailingOpenApi(),
    )

    with pytest.raises(SandboxError, match="无法打开"):
        _windows_file_link_count(tmp_path / "file.txt")


def test_wsb_xml_rejects_oversized_snapshot_file(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    oversized = snapshot / "large.bin"
    with oversized.open("wb") as file:
        file.truncate(MAX_SNAPSHOT_FILE_BYTES + 1)
    spec = RunSpec(
        executable=_absolute_executable(tmp_path),
        policy=SandboxPolicy(workspace_mode="read-only-snapshot"),
        workspace_snapshot=snapshot.resolve(),
    )

    with pytest.raises(SandboxError, match="单文件上限"):
        WindowsSandboxBackend(
            platform_name="nt",
            executable_locator=lambda _: None,
        ).build_config_xml(spec)


def test_wsb_xml_rejects_oversized_snapshot_total(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    file_size = MAX_SNAPSHOT_FILE_BYTES
    file_count = MAX_SNAPSHOT_TOTAL_BYTES // file_size + 1
    for index in range(file_count):
        with (snapshot / f"{index}.bin").open("wb") as file:
            file.truncate(file_size)
    spec = RunSpec(
        executable=_absolute_executable(tmp_path),
        policy=SandboxPolicy(workspace_mode="read-only-snapshot"),
        workspace_snapshot=snapshot.resolve(),
    )

    with pytest.raises(SandboxError, match="累计字节上限"):
        WindowsSandboxBackend(
            platform_name="nt",
            executable_locator=lambda _: None,
        ).build_config_xml(spec)
