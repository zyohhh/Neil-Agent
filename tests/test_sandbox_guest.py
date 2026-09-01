"""Tests for the candidate Windows Sandbox guest protocol and runner."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import subprocess
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import pytest

import neil_agent.sandbox_guest as guest
from neil_agent.sandbox_guest import (
    GUEST_BINARY_FILENAME,
    GUEST_CONTROL_DIRECTORY,
    GUEST_EXECUTE_MODE,
    GUEST_EXPORT_DIRECTORY,
    GUEST_EXPORT_MODE,
    GUEST_PROTOCOL_VERSION,
    GUEST_RUNNER_SECURITY_ASSURANCE,
    GUEST_RUNNER_VERSION,
    GUEST_SNAPSHOT_DIRECTORY,
    SandboxGuestError,
    SandboxGuestRequest,
    build_guest_runner,
    find_dotnet_framework_csc,
    parse_guest_request,
    parse_guest_result,
)

RUN_ID = "a" * 32
INSTANCE_ID = "b" * 32


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _request(**updates: object) -> SandboxGuestRequest:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "instance_id": INSTANCE_ID,
        "snapshot_manifest_sha256": "c" * 64,
        "runner_source_sha256": "d" * 64,
        "approval_binding_sha256": "e" * 64,
        "executable": r"bin\worker.exe",
        "argv": (r'quoted "value"', r"trailing\\"),
        "cwd": "workspace",
        "environment": {"NEIL_MODE": "test"},
        "timeout_ms": 2_000,
        "max_output_bytes": 1_024,
        "active_process_limit": 2,
        "process_memory_bytes": 32 * 1024 * 1024,
        "job_memory_bytes": 64 * 1024 * 1024,
    }
    values.update(updates)
    return SandboxGuestRequest.create(**values)  # type: ignore[arg-type]


def _rehash_request(payload: dict[str, object]) -> bytes:
    hash_payload = dict(payload)
    hash_payload.pop("request_hash", None)
    payload["request_hash"] = _digest(hash_payload)
    return _canonical(payload)


def _result_bytes(
    request: SandboxGuestRequest,
    *,
    stdout: bytes = b"out",
    stderr: bytes = b"err",
    **updates: object,
) -> bytes:
    payload: dict[str, object] = {
        "version": GUEST_PROTOCOL_VERSION,
        "runner_version": GUEST_RUNNER_VERSION,
        "security_assurance": GUEST_RUNNER_SECURITY_ASSURANCE,
        "run_id": request.run_id,
        "request_hash": request.request_hash,
        "instance_id": request.instance_id,
        "status": "exited",
        "exit_code": 0,
        "stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "stderr_b64": base64.b64encode(stderr).decode("ascii"),
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "duration_ms": 10,
        "error_code": None,
        "job_terminated": True,
    }
    payload.update(updates)
    payload["result_hash"] = _digest(payload)
    return _canonical(payload)


def test_request_round_trip_is_canonical_and_hash_bound() -> None:
    request = _request(
        argv=(r'one "two"', r"three\\", "雪"),
        environment={"NEIL_Z": "last", "NEIL_A": "first"},
    )

    encoded = request.canonical_bytes()
    parsed = parse_guest_request(encoded)

    assert parsed == request
    assert parsed.request_hash == _digest(parsed.hash_payload())
    assert encoded == _canonical(json.loads(encoded))
    assert parsed.argv == (r'one "two"', r"three\\", "雪")


@pytest.mark.parametrize(
    "transform",
    [
        lambda raw: raw + b"\n",
        lambda raw: raw.replace(b'"argv":[', b'"argv":[],"argv":[', 1),
    ],
    ids=["noncanonical-whitespace", "duplicate-key"],
)
def test_request_rejects_noncanonical_json(
    transform: Any,
) -> None:
    raw = _request().canonical_bytes()

    with pytest.raises(SandboxGuestError):
        parse_guest_request(transform(raw))


def test_request_rejects_tampering_unknown_fields_and_bool_integers() -> None:
    request = _request()
    payload = json.loads(request.canonical_bytes())
    payload["argv"] = ["tampered"]
    with pytest.raises(SandboxGuestError):
        parse_guest_request(_canonical(payload))

    payload = json.loads(request.canonical_bytes())
    payload["unexpected"] = "rejected"
    with pytest.raises(SandboxGuestError):
        parse_guest_request(_rehash_request(payload))

    payload = json.loads(request.canonical_bytes())
    payload["timeout_ms"] = True
    with pytest.raises(SandboxGuestError):
        parse_guest_request(_rehash_request(payload))


@pytest.mark.parametrize(
    "executable",
    [
        r"..\escape.exe",
        r"C:\absolute.exe",
        "forward/slash.exe",
        r"\\server\share.exe",
        "NUL.exe",
        r"folder\.\worker.exe",
        r"folder\\worker.exe",
        "worker.cmd",
        "worker.exe.",
    ],
)
def test_request_rejects_unsafe_executable_paths(executable: str) -> None:
    with pytest.raises(ValueError):
        _request(executable=executable)


@pytest.mark.parametrize(
    "environment",
    [
        {"PATH": "rejected"},
        {"NEIL_lower": "rejected"},
        {"NEIL_OK": "line\nbreak"},
    ],
)
def test_request_rejects_nonminimal_environment(
    environment: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        _request(environment=environment)


def test_request_rejects_inconsistent_memory_and_post_validation_mutation() -> None:
    with pytest.raises(ValueError):
        _request(
            process_memory_bytes=64 * 1024 * 1024,
            job_memory_bytes=32 * 1024 * 1024,
        )

    request = _request()
    request.environment["NEIL_MODE"] = "mutated"
    with pytest.raises(SandboxGuestError, match="changed after validation"):
        request.canonical_bytes()


def test_result_round_trip_is_bound_bounded_and_job_confirmed() -> None:
    request = _request()

    result = parse_guest_result(
        _result_bytes(request, stdout=b"hello", stderr=b"warning"),
        request=request,
    )

    assert result.stdout == b"hello"
    assert result.stderr == b"warning"
    assert result.job_terminated is True
    assert result.security_assurance == ("certified-windows-sandbox-v1")
    assert result.result_hash == _digest(result.hash_payload())


@pytest.mark.parametrize(
    "updates",
    [
        {"job_terminated": False},
        {"status": "timeout", "exit_code": None, "error_code": "cancelled"},
        {"status": "runner_error", "exit_code": None, "error_code": "timeout"},
        {"status": "exited", "exit_code": None, "error_code": None},
    ],
    ids=[
        "job-not-empty",
        "mismatched-execution-error",
        "mismatched-runner-error",
        "missing-exit-code",
    ],
)
def test_result_rejects_invalid_state(updates: dict[str, object]) -> None:
    request = _request()

    with pytest.raises(SandboxGuestError):
        parse_guest_result(_result_bytes(request, **updates), request=request)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "c" * 32),
        ("request_hash", "d" * 64),
        ("instance_id", "e" * 32),
    ],
)
def test_result_rejects_wrong_request_binding(field: str, value: str) -> None:
    request = _request()

    with pytest.raises(SandboxGuestError, match="not bound"):
        parse_guest_result(
            _result_bytes(request, **{field: value}),
            request=request,
        )


def test_result_rejects_requested_output_overrun_and_tampering() -> None:
    request = _request(max_output_bytes=1_024)
    with pytest.raises(SandboxGuestError, match="requested output limit"):
        parse_guest_result(
            _result_bytes(request, stdout=b"x" * 1_025, stderr=b""),
            request=request,
        )

    raw = _result_bytes(request)
    payload = json.loads(raw)
    payload["duration_ms"] = 11
    with pytest.raises(SandboxGuestError):
        parse_guest_result(_canonical(payload), request=request)


def test_runner_source_keeps_fixed_candidate_security_contract() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "neil_agent"
        / "sandbox_guest_runner.cs"
    ).read_text(encoding="utf-8")

    assert GUEST_BINARY_FILENAME == "neil-sandbox-runner.exe"
    assert GUEST_CONTROL_DIRECTORY == r"C:\NeilAgent\Control"
    assert GUEST_SNAPSHOT_DIRECTORY == r"C:\NeilAgent\Snapshot"
    assert GUEST_EXPORT_DIRECTORY == r"C:\NeilAgent\Export"
    assert f'"{GUEST_EXECUTE_MODE}"' in source
    assert f'"{GUEST_EXPORT_MODE}"' in source
    assert "run_command" not in source
    assert (
        r'private const string RequestPath = @"C:\NeilAgent\Control\request.json"'
        in source
    )
    assert (
        r'private const string ExportPath = @"C:\NeilAgent\Export\result.json"'
        in source
    )

    restrict = source.index("restrictedToken = CreateRestrictedLowIntegrityToken()")
    create = source.index("created = CreateProcessAsUser(")
    assign = source.index("AssignProcessToJobObject(", create)
    resume = source.index("ResumeThread(", assign)
    assert restrict < create < assign < resume
    assert "CreateSuspended" in source[create:assign]
    assert "CreateRestrictedToken(" in source
    assert "DisableMaxPrivilege | SandboxInert | LuaToken" in source
    assert 'ConvertStringSidToSid("S-1-16-4096"' in source
    assert "SetTokenInformation(" in source
    assert 'SecureRunnerDirectory(ResultRoot, "ME")' in source
    assert 'SecureRunnerDirectory(ScratchRoot, "LW")' in source
    assert "JobObjectLimitKillOnJobClose" in source
    assert "JobObjectLimitActiveProcess" in source
    assert "JobObjectLimitProcessMemory" in source
    assert "JobObjectLimitJobMemory" in source
    assert "TerminateAndConfirmEmptyJob" in source
    assert "QueryInformationJobObject" in source
    assert '"job_terminated"' in source

    assert "STARTUPINFOEX" in source
    assert "ProcThreadAttributeHandleList" in source
    assert "UpdateProcThreadAttribute" in source
    assert "new IntPtr[] { nullInput, stdoutWrite, stderrWrite }" in source
    assert "SafeCancellationRequested" in source
    assert "SharedOutputBudget" in source
    assert "stopwatch.ElapsedMilliseconds >= request.TimeoutMs" in source
    assert "WriteAtomic(ExportRoot, ExportPath, resultBytes)" in source


@pytest.mark.windows_sandbox_security
@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
def test_real_runner_reclassifies_fast_flood_after_child_exit(tmp_path: Path) -> None:
    """Cover output overflow discovered only after the direct child has exited."""

    compiler = find_dotnet_framework_csc()
    if compiler is None:
        pytest.skip(".NET Framework csc.exe is unavailable")

    control = tmp_path / "control"
    snapshot = tmp_path / "snapshot"
    scratch = tmp_path / "scratch"
    result_root = tmp_path / "result"
    export = tmp_path / "export"
    control.mkdir()
    snapshot.mkdir()

    request = _request(
        executable="fast-flood.exe",
        argv=(),
        cwd=".",
        environment={},
        timeout_ms=5_000,
        max_output_bytes=1_024,
    )
    (control / "request.json").write_bytes(request.canonical_bytes())

    flood_source = tmp_path / "fast-flood.cs"
    flood_source.write_text(
        """
using System;
using System.IO;

internal static class FastFlood
{
    private static int Main()
    {
        byte[] payload = new byte[1025];
        for (int index = 0; index < payload.Length; index++)
        {
            payload[index] = (byte)'x';
        }
        using (Stream output = Console.OpenStandardOutput())
        {
            output.Write(payload, 0, payload.Length);
            output.Flush();
        }
        return 0;
    }
}
""".lstrip(),
        encoding="utf-8",
    )
    flood_binary = snapshot / "fast-flood.exe"
    _compile_csharp(
        compiler,
        flood_source,
        flood_binary,
        reference=None,
        cwd=tmp_path,
    )

    fixed_source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "neil_agent"
        / "sandbox_guest_runner.cs"
    ).read_text(encoding="utf-8")
    patched_source = _replace_runner_paths(
        fixed_source,
        {
            "ControlRoot": control,
            "SnapshotRoot": snapshot,
            "ScratchRoot": scratch,
            "ResultRoot": result_root,
            "ExportRoot": export,
            "RequestPath": control / "request.json",
            "CancelPath": control / "cancel.signal",
            "ResultPath": result_root / "result.json",
            "MarkerPath": result_root / "complete.marker",
            "ExportPath": export / "result.json",
        },
    )
    runner_source = tmp_path / "sandbox_guest_runner.cs"
    runner_source.write_text(patched_source, encoding="utf-8")
    runner_binary = tmp_path / GUEST_BINARY_FILENAME
    _compile_csharp(
        compiler,
        runner_source,
        runner_binary,
        reference=compiler.parent / "System.Web.Extensions.dll",
        cwd=tmp_path,
    )

    environment = {
        "SystemRoot": r"C:\Windows",
        "WINDIR": r"C:\Windows",
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
    }
    completed = subprocess.run(
        [str(runner_binary), GUEST_EXECUTE_MODE],
        shell=False,
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""
    parsed = parse_guest_result(
        (result_root / "result.json").read_bytes(),
        request=request,
    )
    assert parsed.status == "output_limit"
    assert parsed.error_code == "output_limit"
    assert len(parsed.stdout) + len(parsed.stderr) == request.max_output_bytes
    assert parsed.job_terminated is True


@pytest.mark.windows_sandbox_security
@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
def test_real_runner_cancels_only_after_guest_tree_is_ready(tmp_path: Path) -> None:
    compiler = find_dotnet_framework_csc()
    if compiler is None:
        pytest.skip(".NET Framework csc.exe is unavailable")

    control = tmp_path / "control"
    snapshot = tmp_path / "snapshot"
    scratch = tmp_path / "scratch"
    result_root = tmp_path / "result"
    export = tmp_path / "export"
    control.mkdir()
    snapshot.mkdir()
    request = _request(
        executable="probe.exe",
        argv=("tree",),
        cwd=".",
        environment={},
        timeout_ms=60_000,
        max_output_bytes=16 * 1024,
        active_process_limit=4,
        process_memory_bytes=128 * 1024 * 1024,
        job_memory_bytes=256 * 1024 * 1024,
    )
    (control / "request.json").write_bytes(request.canonical_bytes())

    probe_source = (
        Path(__file__).resolve().parent / "fixtures" / "sandbox_security_probe.cs"
    )
    _compile_csharp(
        compiler,
        probe_source,
        snapshot / "probe.exe",
        reference=None,
        cwd=tmp_path,
    )
    fixed_source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "neil_agent"
        / "sandbox_guest_runner.cs"
    ).read_text(encoding="utf-8")
    patched_source = _replace_runner_paths(
        fixed_source,
        {
            "ControlRoot": control,
            "SnapshotRoot": snapshot,
            "ScratchRoot": scratch,
            "ResultRoot": result_root,
            "ExportRoot": export,
            "RequestPath": control / "request.json",
            "CancelPath": control / "cancel.signal",
            "ResultPath": result_root / "result.json",
            "MarkerPath": result_root / "complete.marker",
            "ExportPath": export / "result.json",
        },
    )
    runner_source = tmp_path / "sandbox_guest_runner.cs"
    runner_source.write_text(patched_source, encoding="utf-8")
    runner_binary = tmp_path / GUEST_BINARY_FILENAME
    _compile_csharp(
        compiler,
        runner_source,
        runner_binary,
        reference=compiler.parent / "System.Web.Extensions.dll",
        cwd=tmp_path,
    )

    process = subprocess.Popen(
        [str(runner_binary), GUEST_EXECUTE_MODE],
        shell=False,
        cwd=tmp_path,
        env={
            "SystemRoot": r"C:\Windows",
            "WINDIR": r"C:\Windows",
            "TEMP": str(tmp_path),
            "TMP": str(tmp_path),
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ready = scratch / "tree-ready.txt"
    deadline = monotonic() + 15.0
    try:
        while not ready.is_file() and monotonic() < deadline:
            sleep(0.02)
        assert ready.is_file(), "guest grandchild never reached its ready boundary"
        grandchild_pid = int(ready.read_text(encoding="utf-8"))
        (control / "cancel.signal").write_bytes(b"cancel")
        stdout, stderr = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 0
    assert stdout == b""
    assert stderr == b""
    parsed = parse_guest_result(
        (result_root / "result.json").read_bytes(),
        request=request,
    )
    assert parsed.status == "cancelled"
    assert parsed.error_code == "cancelled"
    assert parsed.job_terminated is True
    process_ids = {
        int(value)
        for value in re.findall(
            rb"(?:tree-root|child|tree-child|grandchild)=(\d+)", parsed.stdout
        )
    }
    process_ids.add(grandchild_pid)
    assert len(process_ids) >= 3
    assert all(not _windows_process_is_running(pid) for pid in process_ids)


def _replace_runner_paths(source: str, paths: dict[str, Path]) -> str:
    result = source
    for constant, path in paths.items():
        prefix = f"    private const string {constant} = "
        matches = [line for line in result.splitlines() if line.startswith(prefix)]
        assert len(matches) == 1
        escaped_path = str(path).replace('"', '""')
        replacement = f'{prefix}@"{escaped_path}";'
        result = result.replace(matches[0], replacement, 1)
    return result


def _windows_process_is_running(process_id: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    get_exit_code.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = open_process(0x1000, 0, process_id)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_uint32()
        return bool(get_exit_code(handle, ctypes.byref(exit_code))) and (
            exit_code.value == 259
        )
    finally:
        close_handle(handle)


def _compile_csharp(
    compiler: Path,
    source: Path,
    output: Path,
    *,
    reference: Path | None,
    cwd: Path,
) -> None:
    arguments = [
        str(compiler),
        "/nologo",
        "/target:exe",
        "/optimize+",
        "/checked+",
        "/warnaserror+",
        "/debug-",
        "/platform:anycpu",
    ]
    if reference is not None:
        arguments.append(f"/reference:{reference}")
    arguments.extend((f"/out:{output}", str(source)))
    completed = subprocess.run(
        arguments,
        shell=False,
        cwd=cwd,
        env={
            "SystemRoot": r"C:\Windows",
            "WINDIR": r"C:\Windows",
            "TEMP": str(cwd),
            "TMP": str(cwd),
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, (
        completed.stdout.decode(errors="replace"),
        completed.stderr.decode(errors="replace"),
    )
    assert output.is_file()


def _fake_compiler_layout(tmp_path: Path) -> Path:
    compiler = tmp_path / "csc.exe"
    compiler.write_bytes(b"fake compiler")
    (tmp_path / "System.Web.Extensions.dll").write_bytes(b"fake reference")
    return compiler


def test_fixed_system_compiler_validation_allows_nonreparse_hardlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler_origin = tmp_path / "compiler-origin"
    compiler_origin.write_bytes(b"compiler")
    compiler = tmp_path / "csc.exe"
    os.link(compiler_origin, compiler)
    reference_origin = tmp_path / "reference-origin"
    reference_origin.write_bytes(b"reference")
    os.link(reference_origin, tmp_path / "System.Web.Extensions.dll")
    monkeypatch.setattr(guest, "DOTNET_FRAMEWORK_CSC_PATHS", (compiler,))

    assert compiler.stat().st_nlink > 1
    assert find_dotnet_framework_csc() == compiler


def test_build_runner_uses_fixed_argv_minimal_environment_and_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler = _fake_compiler_layout(tmp_path)
    monkeypatch.setattr(guest, "DOTNET_FRAMEWORK_CSC_PATHS", (compiler,))
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        output = next(item[5:] for item in argv if item.startswith("/out:"))
        Path(output).write_bytes(b"MZ fixed runner")
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    with build_guest_runner(_compiler_runner=fake_run) as build:
        assert build.compiler_path == compiler
        assert build.source_path.name == "sandbox_guest_runner.cs"
        assert build.binary_path.name == GUEST_BINARY_FILENAME
        assert build.source_sha256 == sha256(build.source_path.read_bytes()).hexdigest()
        assert build.binary_sha256 == sha256(b"MZ fixed runner").hexdigest()
        assert build.binary_path.read_bytes() == b"MZ fixed runner"

        argv, kwargs = calls[0]
        assert argv == [
            str(compiler),
            "/nologo",
            "/target:exe",
            "/optimize+",
            "/checked+",
            "/warnaserror+",
            "/debug-",
            "/platform:anycpu",
            f"/reference:{tmp_path / 'System.Web.Extensions.dll'}",
            f"/out:{build.binary_path}",
            str(build.source_path),
        ]
        assert kwargs["shell"] is False
        assert kwargs["cwd"] == build.source_path.parent
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        assert kwargs["timeout"] == 60
        assert kwargs["check"] is False
        assert kwargs["env"] == {
            "SystemRoot": r"C:\Windows",
            "WINDIR": r"C:\Windows",
            "TEMP": str(build.source_path.parent),
            "TMP": str(build.source_path.parent),
        }


def test_build_runner_rejects_hardlinked_build_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler = _fake_compiler_layout(tmp_path)
    monkeypatch.setattr(guest, "DOTNET_FRAMEWORK_CSC_PATHS", (compiler,))

    def hardlink_output(
        argv: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        output = Path(next(item[5:] for item in argv if item.startswith("/out:")))
        output.write_bytes(b"MZ linked runner")
        os.link(output, output.with_suffix(".linked"))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    with pytest.raises(SandboxGuestError, match="non-linked"):
        with build_guest_runner(_compiler_runner=hardlink_output):
            pytest.fail("unreachable")


def test_build_runner_fails_closed_without_compiler_or_on_compile_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        guest,
        "DOTNET_FRAMEWORK_CSC_PATHS",
        (tmp_path / "missing-csc.exe",),
    )
    with pytest.raises(SandboxGuestError, match="compiler is unavailable"):
        with build_guest_runner():
            pytest.fail("unreachable")

    compiler = _fake_compiler_layout(tmp_path)
    monkeypatch.setattr(guest, "DOTNET_FRAMEWORK_CSC_PATHS", (compiler,))

    def fail_compile(
        argv: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"failure")

    with pytest.raises(SandboxGuestError, match="exit code 1"):
        with build_guest_runner(_compiler_runner=fail_compile):
            pytest.fail("unreachable")


def test_real_framework_compiler_builds_and_invalid_mode_fails_closed() -> None:
    if find_dotnet_framework_csc() is None:
        pytest.skip(".NET Framework csc.exe is unavailable")

    with build_guest_runner() as build:
        environment = {
            "SystemRoot": r"C:\Windows",
            "WINDIR": r"C:\Windows",
            "TEMP": str(build.binary_path.parent),
            "TMP": str(build.binary_path.parent),
        }
        completed = subprocess.run(
            [str(build.binary_path), "invalid"],
            shell=False,
            cwd=build.binary_path.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

        assert completed.returncode == 64
        assert build.source_sha256 == sha256(build.source_path.read_bytes()).hexdigest()
        assert build.binary_sha256 == sha256(build.binary_path.read_bytes()).hexdigest()
