"""Mandatory attack-oriented tests for a real Windows Sandbox installation.

Normal developer machines may skip this module when ``wsb.exe`` or the fixed
runner compiler is unavailable.  The dedicated security workflow sets
``SANDBOX_REQUIRED=1``; in that mode every missing prerequisite is a failure.
"""

from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
from time import sleep
from typing import cast
from uuid import uuid4

import pytest

from neil_agent.sandbox import CancellationSignal
from neil_agent.sandbox_approval import RunCommandApprovalBinding
from neil_agent.sandbox_evidence import (
    RawObservationRecorder,
    SandboxEvidenceError,
    collect_evidence_subject,
    collect_windows_platform_fingerprint,
    ensure_canonical_evidence_file,
)
from neil_agent.sandbox_guest import (
    GUEST_BINARY_FILENAME,
    GUEST_SOURCE_FILENAME,
    GUEST_REQUEST_FILENAME,
    GuestRunnerBuild,
    SandboxGuestRequest,
    build_guest_runner,
    find_dotnet_framework_csc,
)
from neil_agent.sandbox_snapshot import prepare_snapshot
from neil_agent.windows_sandbox import (
    WSB_RUNNER_COMMAND,
    BoundedSubprocessCliRunner,
    WsbCliCompleted,
    WsbCliRunner,
    WsbExecutionPlan,
    WsbExecutionResult,
    WsbHostExecutionError,
    WsbHostExecutor,
    WsbRawObservation,
    WsbRawObserver,
)

pytestmark = pytest.mark.windows_sandbox_security

_REQUIRED = os.environ.get("SANDBOX_REQUIRED") == "1"
_EVIDENCE_REQUIRED = os.environ.get("SANDBOX_EVIDENCE_REQUIRED") == "1"
_PROBE_SOURCE = Path(__file__).parent / "fixtures" / "sandbox_security_probe.cs"
_MAX_COMPILER_OUTPUT_BYTES = 64 * 1024
_MAX_BUILD_ARTIFACT_BYTES = 128 * 1024 * 1024


def _missing_prerequisite(reason: str) -> None:
    if _REQUIRED:
        pytest.fail(f"mandatory Windows Sandbox prerequisite failed: {reason}")
    pytest.skip(reason)


def _require_host_network_positive_controls() -> None:
    """Prevent a disconnected security runner from proving guest isolation."""

    controls = (
        (socket.AF_INET, ("1.1.1.1", 443), "IPv4 TCP"),
        (
            socket.AF_INET6,
            ("2606:4700:4700::1111", 443, 0, 0),
            "IPv6 TCP",
        ),
    )
    for family, endpoint, label in controls:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as connection:
                connection.settimeout(5.0)
                connection.connect(endpoint)
        except OSError:
            _missing_prerequisite(f"the host {label} positive control is not reachable")
    try:
        addresses = socket.getaddrinfo(
            "example.com",
            443,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        addresses = []
    if not addresses:
        _missing_prerequisite("the host DNS positive control is unavailable")


def _required_evidence_path(variable: str) -> Path:
    value = os.environ.get(variable)
    if not value:
        pytest.fail(f"mandatory evidence path is missing: {variable}")
    path = Path(value)
    if not path.is_absolute():
        pytest.fail(f"mandatory evidence path is not absolute: {variable}")
    return path


def _evidence_build_root() -> Path:
    root = _required_evidence_path("SANDBOX_EVIDENCE_ROOT")
    build_root = _required_evidence_path("SANDBOX_EVIDENCE_BUILD_ROOT")
    try:
        root_metadata = root.lstat()
        build_metadata = build_root.lstat()
        resolved_root = root.resolve(strict=True)
        resolved_build = build_root.resolve(strict=True)
        resolved_build.relative_to(resolved_root)
    except (OSError, ValueError):
        pytest.fail("mandatory evidence build root is invalid")
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    if (
        root.is_symlink()
        or build_root.is_symlink()
        or int(getattr(root_metadata, "st_file_attributes", 0)) & reparse
        or int(getattr(build_metadata, "st_file_attributes", 0)) & reparse
        or not resolved_root.is_dir()
        or not resolved_build.is_dir()
    ):
        pytest.fail("mandatory evidence roots must be existing directories")
    return resolved_build


def _require_safe_build_directory(path: Path, root: Path) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        pytest.fail("shared build directory is invalid")
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    if (
        path.is_symlink()
        or int(getattr(metadata, "st_file_attributes", 0)) & reparse
        or not resolved.is_dir()
    ):
        pytest.fail("shared build directory is a reparse point or non-directory")


def _artifact_sha256(path: Path) -> str:
    try:
        metadata = path.stat()
        if (
            not path.is_file()
            or path.is_symlink()
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_BUILD_ARTIFACT_BYTES
        ):
            pytest.fail(f"shared build artifact is invalid: {path.name}")
        digest = sha256()
        total = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > _MAX_BUILD_ARTIFACT_BYTES:
                    pytest.fail(f"shared build artifact is too large: {path.name}")
                digest.update(chunk)
        if total != metadata.st_size:
            pytest.fail(f"shared build artifact changed while read: {path.name}")
        return digest.hexdigest()
    except OSError:
        pytest.fail(f"shared build artifact is unavailable: {path.name}")


def _copy_artifact_exclusively(source: Path, target: Path) -> None:
    try:
        payload = source.read_bytes()
        if not payload or len(payload) > _MAX_BUILD_ARTIFACT_BYTES:
            pytest.fail(f"built artifact has an invalid size: {source.name}")
        with target.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        pytest.fail(f"built artifact could not be persisted: {target.name}")


@pytest.fixture(scope="session")
def wsb_cli() -> Path:
    if os.name != "nt":
        _missing_prerequisite("the host platform is not Windows")
    located = shutil.which("wsb.exe")
    if located is None:
        _missing_prerequisite("wsb.exe is unavailable")
    executable = Path(located).resolve()
    if executable.name.casefold() != "wsb.exe" or not executable.is_file():
        _missing_prerequisite("the located WSB CLI is not a regular wsb.exe")
    return executable


@pytest.fixture(scope="session")
def guest_runner(wsb_cli: Path) -> Iterator[GuestRunnerBuild]:
    del wsb_cli
    compiler = find_dotnet_framework_csc()
    if compiler is None:
        _missing_prerequisite("the fixed .NET Framework C# compiler is unavailable")
    if not _EVIDENCE_REQUIRED:
        with build_guest_runner() as build:
            yield build
        return

    evidence_build_root = _evidence_build_root()
    build_root = evidence_build_root / "guest-runner"
    build_root.mkdir(parents=True, exist_ok=True)
    _require_safe_build_directory(build_root, evidence_build_root)
    source = build_root / GUEST_SOURCE_FILENAME
    binary = build_root / GUEST_BINARY_FILENAME
    if source.exists() != binary.exists():
        pytest.fail("shared guest runner build is incomplete")
    if not source.exists():
        with build_guest_runner() as ephemeral:
            _copy_artifact_exclusively(ephemeral.source_path, source)
            _copy_artifact_exclusively(ephemeral.binary_path, binary)
    yield GuestRunnerBuild(
        compiler_path=compiler,
        source_path=source,
        binary_path=binary,
        source_sha256=_artifact_sha256(source),
        binary_sha256=_artifact_sha256(binary),
    )


@pytest.fixture(scope="session")
def security_probe(
    wsb_cli: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    del wsb_cli
    compiler = find_dotnet_framework_csc()
    if compiler is None:
        _missing_prerequisite("the fixed .NET Framework C# compiler is unavailable")
    if not _PROBE_SOURCE.is_file():
        pytest.fail("the fixed sandbox security probe source is missing")
    if _EVIDENCE_REQUIRED:
        evidence_build_root = _evidence_build_root()
        build_root = evidence_build_root / "security-probe"
        build_root.mkdir(parents=True, exist_ok=True)
        _require_safe_build_directory(build_root, evidence_build_root)
    else:
        build_root = tmp_path_factory.mktemp("sandbox-security-probe")
    binary = build_root / "sandbox-security-probe.exe"
    if binary.exists():
        _artifact_sha256(binary)
        return binary
    environment = {
        "SystemRoot": r"C:\Windows",
        "WINDIR": r"C:\Windows",
        "TEMP": str(build_root),
        "TMP": str(build_root),
    }
    completed = subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:exe",
            "/optimize+",
            "/checked+",
            "/warnaserror+",
            "/debug-",
            "/platform:anycpu",
            f"/out:{binary}",
            str(_PROBE_SOURCE),
        ],
        shell=False,
        cwd=build_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if (
        completed.returncode != 0
        or len(completed.stdout) > _MAX_COMPILER_OUTPUT_BYTES
        or len(completed.stderr) > _MAX_COMPILER_OUTPUT_BYTES
        or not binary.is_file()
    ):
        pytest.fail("the fixed sandbox security probe did not compile safely")
    return binary


@pytest.fixture(scope="session")
def evidence_raw_observer(
    wsb_cli: Path,
    guest_runner: GuestRunnerBuild,
    security_probe: Path,
) -> Iterator[WsbRawObserver | None]:
    """Persist exact WSB stdout and bind it to one immutable run identity."""

    if not _EVIDENCE_REQUIRED:
        yield None
        return
    raw_path = _required_evidence_path("SANDBOX_EVIDENCE_RAW_JSONL")
    platform_path = _required_evidence_path("SANDBOX_EVIDENCE_PLATFORM_JSON")
    subject_path = _required_evidence_path("SANDBOX_EVIDENCE_SUBJECT_JSON")
    wheel_path = _required_evidence_path("SANDBOX_EVIDENCE_WHEEL")
    repeat_id = os.environ.get("SANDBOX_EVIDENCE_REPEAT_ID", "")
    execution_nonce = os.environ.get("SANDBOX_EVIDENCE_EXECUTION_NONCE", "")
    git_commit_sha = os.environ.get("GITHUB_SHA", "")
    repository_root = Path(__file__).parents[1].resolve(strict=True)
    try:
        platform = collect_windows_platform_fingerprint(wsb_cli)
        subject = collect_evidence_subject(
            repository_root=repository_root,
            git_commit_sha=git_commit_sha,
            wheel_path=wheel_path,
            runner_source_path=guest_runner.source_path,
            runner_binary_path=guest_runner.binary_path,
            compiler_path=guest_runner.compiler_path,
            probe_binary_path=security_probe,
        )
        ensure_canonical_evidence_file(platform_path, platform)
        ensure_canonical_evidence_file(subject_path, subject)
    except SandboxEvidenceError as error:
        pytest.fail(f"mandatory evidence identity failed: {error}")

    with RawObservationRecorder(
        raw_path,
        repeat_id=repeat_id,
        execution_nonce=execution_nonce,
    ) as recorder:

        def observe(observation: WsbRawObservation) -> None:
            # Record every completion before the host parser sees it.
            completed = observation.completed
            recorder.record(
                observation.stage,
                completed.stdout,
                argv=observation.argv,
                instance_id=str(observation.instance_id),
                run_id=str(observation.run_id),
                request_hash=observation.request_hash,
                returncode=completed.returncode,
                timed_out=completed.timed_out,
                cancelled=completed.cancelled,
                output_limited=completed.output_limited,
            )

        yield cast(WsbRawObserver, observe)


@dataclass(frozen=True, slots=True)
class _RunArtifacts:
    result: WsbExecutionResult
    source: Path


def _execute_probe(
    tmp_path: Path,
    *,
    wsb_cli: Path,
    guest_runner: GuestRunnerBuild,
    security_probe: Path,
    mode: str,
    arguments: tuple[str, ...] = (),
    timeout_ms: int = 10_000,
    max_output_bytes: int = 32 * 1024,
    active_process_limit: int = 4,
    process_memory_bytes: int = 128 * 1024 * 1024,
    job_memory_bytes: int = 256 * 1024 * 1024,
    executor_runner: WsbCliRunner | None = None,
    raw_observer: WsbRawObserver | None = None,
    cancel: CancellationSignal | None = None,
) -> _RunArtifacts:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source"
    source.mkdir()
    shutil.copyfile(security_probe, source / "probe.exe")
    (source / "visible.txt").write_text("host-original", encoding="utf-8")
    (source / ".env").write_text("DEEPSEEK_API_KEY=host-secret", encoding="utf-8")
    git_directory = source / ".git"
    git_directory.mkdir()
    (git_directory / "config").write_text("credential", encoding="utf-8")

    snapshot_path = tmp_path / "snapshot"
    with prepare_snapshot(source.resolve(), snapshot_path.resolve()) as snapshot:
        assert not (snapshot.root / ".env").exists()
        assert not (snapshot.root / ".git").exists()
        control = tmp_path / "control"
        temporary_root = tmp_path / "host-transport"
        control.mkdir()
        temporary_root.mkdir()
        shutil.copyfile(
            guest_runner.binary_path,
            control / GUEST_BINARY_FILENAME,
        )
        instance_id = uuid4()
        run_id = uuid4()
        approval_binding = RunCommandApprovalBinding(
            executable="probe.exe",
            argv=(mode, *arguments),
            snapshot_manifest_sha256=snapshot.manifest.digest,
            runner_source_sha256=guest_runner.source_sha256,
            runner_binary_sha256=guest_runner.binary_sha256,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
            active_process_limit=active_process_limit,
            process_memory_bytes=process_memory_bytes,
            job_memory_bytes=job_memory_bytes,
        )
        request = SandboxGuestRequest.create(
            run_id=run_id.hex,
            instance_id=instance_id.hex,
            snapshot_manifest_sha256=snapshot.manifest.digest,
            runner_source_sha256=guest_runner.source_sha256,
            approval_binding_sha256=approval_binding.digest,
            executable="probe.exe",
            argv=(mode, *arguments),
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
            active_process_limit=active_process_limit,
            process_memory_bytes=process_memory_bytes,
            job_memory_bytes=job_memory_bytes,
        )
        (control / GUEST_REQUEST_FILENAME).write_bytes(request.canonical_bytes())
        plan = WsbExecutionPlan(
            instance_id=instance_id,
            run_id=run_id,
            request_hash=request.request_hash,
            snapshot_directory=snapshot.root,
            control_directory=control.resolve(),
            temporary_root=temporary_root.resolve(),
            snapshot_manifest_sha256=snapshot.manifest.digest,
            runner_source_sha256=guest_runner.source_sha256,
            runner_sha256=guest_runner.binary_sha256,
            approval_binding_version=request.approval_binding_version,
            approval_binding_sha256=approval_binding.digest,
            timeout_seconds=max(60.0, (timeout_ms / 1_000) + 30.0),
        )
        executor = WsbHostExecutor(
            wsb_cli,
            cli_runner=executor_runner,
            raw_observer=raw_observer,
        )
        result = executor.execute(plan, cancel=cancel)
    return _RunArtifacts(result=result, source=source)


def test_real_wsb_blocks_host_files_network_and_workspace_writeback(
    tmp_path: Path,
    wsb_cli: Path,
    guest_runner: GuestRunnerBuild,
    security_probe: Path,
    evidence_raw_observer: WsbRawObserver | None,
) -> None:
    _require_host_network_positive_controls()
    sentinel = tmp_path / "host-sentinel.txt"
    credential = tmp_path / "host-credential.pem"
    sentinel.write_text("sentinel-secret", encoding="utf-8")
    credential.write_text("credential-secret", encoding="utf-8")
    artifacts = _execute_probe(
        tmp_path / "run",
        wsb_cli=wsb_cli,
        guest_runner=guest_runner,
        security_probe=security_probe,
        raw_observer=evidence_raw_observer,
        mode="isolation",
        arguments=(
            str(sentinel),
            str(credential),
            r"C:\NeilAgent\Snapshot\.env",
            r"C:\NeilAgent\Snapshot\.git\config",
        ),
    )

    result = artifacts.result
    assert result.status == "exited"
    assert result.exit_code == 0
    assert result.error_code is None
    assert result.stdout.strip() == b"isolation-ok"
    assert result.job_terminated is True
    assert sentinel.read_text(encoding="utf-8") == "sentinel-secret"
    assert credential.read_text(encoding="utf-8") == "credential-secret"
    assert (artifacts.source / "visible.txt").read_text(encoding="utf-8") == (
        "host-original"
    )
    assert not (artifacts.source / "sandbox-must-not-write.txt").exists()


def test_real_wsb_restricts_low_integrity_child_and_protects_runner_result(
    tmp_path: Path,
    wsb_cli: Path,
    guest_runner: GuestRunnerBuild,
    security_probe: Path,
    evidence_raw_observer: WsbRawObserver | None,
) -> None:
    result = _execute_probe(
        tmp_path,
        wsb_cli=wsb_cli,
        guest_runner=guest_runner,
        security_probe=security_probe,
        raw_observer=evidence_raw_observer,
        mode="token-boundary",
    ).result

    assert result.status == "exited"
    assert result.exit_code == 0
    assert result.stdout.strip() == b"token-boundary-ok"
    assert result.job_terminated is True


def test_real_wsb_blocks_scm_task_scheduler_and_wmi_broker_escape(
    tmp_path: Path,
    wsb_cli: Path,
    guest_runner: GuestRunnerBuild,
    security_probe: Path,
    evidence_raw_observer: WsbRawObserver | None,
) -> None:
    result = _execute_probe(
        tmp_path,
        wsb_cli=wsb_cli,
        guest_runner=guest_runner,
        security_probe=security_probe,
        raw_observer=evidence_raw_observer,
        mode="broker-escape",
        timeout_ms=45_000,
    ).result

    assert result.status == "exited"
    assert result.exit_code == 0
    assert result.stdout.strip() == b"broker-escape-blocked"
    assert result.job_terminated is True


def test_real_wsb_job_denies_breakaway_process_creation(
    tmp_path: Path,
    wsb_cli: Path,
    guest_runner: GuestRunnerBuild,
    security_probe: Path,
    evidence_raw_observer: WsbRawObserver | None,
) -> None:
    result = _execute_probe(
        tmp_path,
        wsb_cli=wsb_cli,
        guest_runner=guest_runner,
        security_probe=security_probe,
        raw_observer=evidence_raw_observer,
        mode="breakaway",
    ).result

    assert result.status == "exited"
    assert result.exit_code == 0
    assert result.stdout.strip() == b"breakaway-blocked"
    assert result.job_terminated is True


def test_real_wsb_kills_child_and_grandchild_on_timeout(
    tmp_path: Path,
    wsb_cli: Path,
    guest_runner: GuestRunnerBuild,
    security_probe: Path,
    evidence_raw_observer: WsbRawObserver | None,
) -> None:
    result = _execute_probe(
        tmp_path,
        wsb_cli=wsb_cli,
        guest_runner=guest_runner,
        security_probe=security_probe,
        raw_observer=evidence_raw_observer,
        mode="tree",
        timeout_ms=5_000,
        max_output_bytes=16 * 1024,
        active_process_limit=4,
    ).result

    assert result.status == "timeout"
    assert result.error_code == "timeout"
    assert result.job_terminated is True
    assert b"tree-root=" in result.stdout
    assert b"tree-child=" in result.stdout
    assert b"grandchild=" in result.stdout


class _CancelDuringGuestExecution:
    def __init__(self, signal: Event) -> None:
        self._delegate = BoundedSubprocessCliRunner()
        self._signal = signal
        self.stop_completed = False

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
        environment: Mapping[str, str],
        cancel: CancellationSignal | None,
    ) -> WsbCliCompleted:
        is_runner = len(argv) > 2 and argv[1] == "exec" and WSB_RUNNER_COMMAND in argv
        if is_runner:
            Thread(target=self._cancel_soon, daemon=True).start()
        completed = self._delegate.run(
            argv,
            timeout_seconds=timeout_seconds,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
            environment=environment,
            cancel=cancel,
        )
        if len(argv) > 1 and argv[1] == "stop":
            self.stop_completed = True
        return completed

    def _cancel_soon(self) -> None:
        # This remains a controller cancellation/strict-stop smoke test because
        # wsb.exe intentionally exposes no live guest process I/O.  Process-tree
        # cleanup is proven separately by the timeout result exported by runner.
        sleep(5.0)
        self._signal.set()


def test_real_wsb_host_cancellation_stops_the_explicit_instance(
    tmp_path: Path,
    wsb_cli: Path,
    guest_runner: GuestRunnerBuild,
    security_probe: Path,
    evidence_raw_observer: WsbRawObserver | None,
) -> None:
    cancel = Event()
    runner = _CancelDuringGuestExecution(cancel)

    with pytest.raises(WsbHostExecutionError, match="取消"):
        _execute_probe(
            tmp_path,
            wsb_cli=wsb_cli,
            guest_runner=guest_runner,
            security_probe=security_probe,
            raw_observer=evidence_raw_observer,
            mode="tree",
            timeout_ms=120_000,
            executor_runner=runner,
            cancel=cancel,
        )
    assert runner.stop_completed is True


def test_real_wsb_bounds_output_while_it_is_read(
    tmp_path: Path,
    wsb_cli: Path,
    guest_runner: GuestRunnerBuild,
    security_probe: Path,
    evidence_raw_observer: WsbRawObserver | None,
) -> None:
    output_limit = 12 * 1024
    result = _execute_probe(
        tmp_path,
        wsb_cli=wsb_cli,
        guest_runner=guest_runner,
        security_probe=security_probe,
        raw_observer=evidence_raw_observer,
        mode="flood",
        timeout_ms=10_000,
        max_output_bytes=output_limit,
    ).result

    assert result.status == "output_limit"
    assert result.error_code == "output_limit"
    assert len(result.stdout) + len(result.stderr) <= output_limit
    assert result.job_terminated is True


def test_real_wsb_enforces_process_memory_limit(
    tmp_path: Path,
    wsb_cli: Path,
    guest_runner: GuestRunnerBuild,
    security_probe: Path,
    evidence_raw_observer: WsbRawObserver | None,
) -> None:
    result = _execute_probe(
        tmp_path,
        wsb_cli=wsb_cli,
        guest_runner=guest_runner,
        security_probe=security_probe,
        raw_observer=evidence_raw_observer,
        mode="memory",
        timeout_ms=20_000,
        process_memory_bytes=64 * 1024 * 1024,
        job_memory_bytes=256 * 1024 * 1024,
    ).result

    assert result.job_terminated is True
    if result.status == "exited":
        assert result.exit_code == 0
        assert b"memory-limit-observed" in result.stdout
    else:
        assert result.status == "resource_limit"
        assert result.error_code == "resource_limit"


def test_real_wsb_enforces_aggregate_job_memory_limit(
    tmp_path: Path,
    wsb_cli: Path,
    guest_runner: GuestRunnerBuild,
    security_probe: Path,
    evidence_raw_observer: WsbRawObserver | None,
) -> None:
    result = _execute_probe(
        tmp_path,
        wsb_cli=wsb_cli,
        guest_runner=guest_runner,
        security_probe=security_probe,
        raw_observer=evidence_raw_observer,
        mode="job-memory",
        timeout_ms=25_000,
        active_process_limit=4,
        process_memory_bytes=96 * 1024 * 1024,
        job_memory_bytes=120 * 1024 * 1024,
    ).result

    assert result.job_terminated is True
    if result.status == "exited":
        assert result.exit_code == 0
        assert b"job-memory-limit-observed=" in result.stdout
    else:
        assert result.status == "resource_limit"
        assert result.error_code == "resource_limit"


def test_real_wsb_enforces_active_process_limit(
    tmp_path: Path,
    wsb_cli: Path,
    guest_runner: GuestRunnerBuild,
    security_probe: Path,
    evidence_raw_observer: WsbRawObserver | None,
) -> None:
    result = _execute_probe(
        tmp_path,
        wsb_cli=wsb_cli,
        guest_runner=guest_runner,
        security_probe=security_probe,
        raw_observer=evidence_raw_observer,
        mode="process-limit",
        timeout_ms=10_000,
        active_process_limit=4,
    ).result

    assert result.status == "exited"
    assert result.exit_code == 0
    assert b"process-limit-observed=true" in result.stdout
    assert result.job_terminated is True
