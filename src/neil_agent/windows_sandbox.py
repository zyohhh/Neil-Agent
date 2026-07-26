"""Candidate host executor for the Windows Sandbox ``wsb.exe`` CLI.

The executor in this module is intentionally not registered as an Agent tool
and does not change the readiness of :mod:`neil_agent.sandbox`.  It models the
host-side sequence needed by a future audited guest runner:

1. start an explicitly named instance with read-only snapshot/control mappings;
2. execute one fixed runner command and wait for its zero exit status;
3. share a new, empty host output directory as writable;
4. execute one fixed exporter command;
5. parse one bounded result envelope bound to the request and instance;
6. stop the explicit instance on every path.

Windows Sandbox currently exposes no process I/O for ``wsb exec``.  The fixed
runner's zero exit status is therefore the first assertion that its complete
guest Job is empty; the exported envelope must assert ``job_terminated`` again.
No user command or argument is ever interpolated into a host CLI argument.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import Literal, Protocol
from uuid import RFC_4122, UUID
from xml.etree import ElementTree

from .errors import SandboxError
from .sandbox import CancellationSignal
from .sandbox_guest import (
    GUEST_BINARY_FILENAME,
    GUEST_CONTROL_DIRECTORY,
    GUEST_EXECUTE_MODE,
    GUEST_EXPORT_DIRECTORY,
    GUEST_EXPORT_MODE,
    GUEST_REQUEST_FILENAME,
    GUEST_RESULT_FILENAME,
    GUEST_SNAPSHOT_DIRECTORY,
    MAX_REQUEST_BYTES,
    MAX_RESULT_BYTES,
    GuestErrorCode,
    GuestStatus,
    SandboxGuestError,
    SandboxGuestRequest,
    parse_guest_request,
    parse_guest_result,
)

WSB_GUEST_SNAPSHOT = GUEST_SNAPSHOT_DIRECTORY
WSB_GUEST_CONTROL = GUEST_CONTROL_DIRECTORY
WSB_GUEST_EXPORT = GUEST_EXPORT_DIRECTORY
WSB_RUNNER_FILENAME = GUEST_BINARY_FILENAME
WSB_REQUEST_FILENAME = GUEST_REQUEST_FILENAME
WSB_RESULT_FILENAME = GUEST_RESULT_FILENAME
WSB_RUNNER_COMMAND = (
    rf'"{WSB_GUEST_CONTROL}\{WSB_RUNNER_FILENAME}" {GUEST_EXECUTE_MODE}'
)
WSB_EXPORTER_COMMAND = (
    rf'"{WSB_GUEST_CONTROL}\{WSB_RUNNER_FILENAME}" {GUEST_EXPORT_MODE}'
)

MAX_CLI_STDOUT_BYTES = 32 * 1024
MAX_CLI_STDERR_BYTES = 16 * 1024
MAX_RESULT_JSON_BYTES = MAX_RESULT_BYTES
MAX_REQUEST_JSON_BYTES = MAX_REQUEST_BYTES
MAX_RUNNER_BYTES = 64 * 1024 * 1024
MAX_TREE_ENTRIES = 100_000
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_ITEMS = 4_096
MAX_CLI_TIMEOUT_SECONDS = 3_600.0
STOP_TIMEOUT_SECONDS = 30.0
PROCESS_POLL_SECONDS = 0.02
PROCESS_TERMINATE_GRACE_SECONDS = 2.0

_HEX_DIGITS = frozenset("0123456789abcdef")
_REPARSE_POINT_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
_BLOCKED_DIRECTORIES = frozenset(
    {
        ".agents",
        ".aws",
        ".azure",
        ".codex",
        ".docker",
        ".git",
        ".gnupg",
        ".kube",
        ".neil-agent",
        ".ssh",
        ".venv",
        "appdata",
    }
)
_BLOCKED_FILE_NAMES = frozenset(
    {
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "application_default_credentials.json",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
_BLOCKED_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
_CLI_STAGE_ALLOWED_STATUSES: dict[str, frozenset[str]] = {
    "start": frozenset({"Running", "Started", "Succeeded"}),
    "runner": frozenset({"Running", "Succeeded"}),
    "share": frozenset({"Running", "Shared", "Succeeded"}),
    "exporter": frozenset({"Running", "Succeeded"}),
    "stop": frozenset({"Stopped", "Succeeded"}),
}


class WsbHostExecutionError(SandboxError):
    """The host-side WSB state machine failed and returned no trusted result."""


@dataclass(frozen=True, slots=True)
class WsbExecutionPlan:
    """One fully bound host-side Windows Sandbox candidate run."""

    instance_id: UUID
    run_id: UUID
    request_hash: str
    snapshot_directory: Path
    control_directory: Path
    temporary_root: Path
    runner_sha256: str
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        _validate_uuid4("instance ID", self.instance_id)
        _validate_uuid4("run ID", self.run_id)
        if self.instance_id == self.run_id:
            raise ValueError("instance ID and run ID must be distinct")
        _validate_digest("request hash", self.request_hash)
        _validate_digest("runner SHA-256", self.runner_sha256)
        for name, value in (
            ("snapshot directory", self.snapshot_directory),
            ("control directory", self.control_directory),
            ("temporary root", self.temporary_root),
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"{name} must be an absolute Path")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0.1 <= self.timeout_seconds <= MAX_CLI_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "WSB timeout must be between 0.1 and "
                f"{MAX_CLI_TIMEOUT_SECONDS:g} seconds"
            )


@dataclass(frozen=True, slots=True)
class WsbExecutionResult:
    """Strict, bounded result exported by the trusted guest helper."""

    instance_id: UUID
    run_id: UUID
    request_hash: str
    status: GuestStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    duration_ms: int
    error_code: GuestErrorCode | None
    result_hash: str
    job_terminated: bool


@dataclass(frozen=True, slots=True)
class WsbCliCompleted:
    """Bounded result from one host-side ``wsb.exe`` invocation."""

    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    cancelled: bool = False
    output_limited: bool = False

    def __post_init__(self) -> None:
        flags = sum((self.timed_out, self.cancelled, self.output_limited))
        if flags > 1:
            raise ValueError("one WSB CLI call cannot have multiple termination flags")
        if flags and self.returncode is not None:
            raise ValueError("terminated WSB CLI calls cannot report an exit code")
        if not flags and (
            isinstance(self.returncode, bool) or not isinstance(self.returncode, int)
        ):
            raise ValueError("completed WSB CLI calls require an integer exit code")
        if not isinstance(self.stdout, bytes) or not isinstance(self.stderr, bytes):
            raise ValueError("WSB CLI output must be bytes")
        if len(self.stdout) > MAX_CLI_STDOUT_BYTES:
            raise ValueError("WSB CLI stdout exceeds its global boundary")
        if len(self.stderr) > MAX_CLI_STDERR_BYTES:
            raise ValueError("WSB CLI stderr exceeds its global boundary")


class WsbCliRunner(Protocol):
    """Injectable process boundary used by :class:`WsbHostExecutor`."""

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
        """Run exactly one host CLI argv without a command shell."""


PopenFactory = Callable[..., subprocess.Popen[bytes]]


class _BoundedCollector:
    __slots__ = ("_data", "_failed", "_limit", "_lock", "_overflow")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()
        self._overflow = Event()
        self._failed = Event()
        self._lock = Lock()

    @property
    def overflowed(self) -> bool:
        return self._overflow.is_set()

    @property
    def failed(self) -> bool:
        return self._failed.is_set()

    def drain(self, stream: object) -> None:
        read = getattr(stream, "read", None)
        if not callable(read):
            self._failed.set()
            return
        try:
            while True:
                chunk = read(4_096)
                if not chunk:
                    return
                if not isinstance(chunk, bytes):
                    self._failed.set()
                    return
                with self._lock:
                    remaining = self._limit - len(self._data)
                    if remaining > 0:
                        self._data.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        self._overflow.set()
        except OSError:
            self._failed.set()

    def bytes(self) -> bytes:
        with self._lock:
            return bytes(self._data)


class BoundedSubprocessCliRunner:
    """Run the WSB host CLI with bounded streaming capture and no shell."""

    __slots__ = ("_popen",)

    def __init__(self, *, popen_factory: PopenFactory | None = None) -> None:
        self._popen = subprocess.Popen if popen_factory is None else popen_factory

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
        if not argv or any(not isinstance(item, str) or "\0" in item for item in argv):
            raise WsbHostExecutionError("WSB CLI argv 无效，未启动进程。")
        if not 0 < stdout_limit <= MAX_CLI_STDOUT_BYTES:
            raise ValueError("WSB CLI stdout limit is invalid")
        if not 0 < stderr_limit <= MAX_CLI_STDERR_BYTES:
            raise ValueError("WSB CLI stderr limit is invalid")
        if not 0 < timeout_seconds <= MAX_CLI_TIMEOUT_SECONDS:
            raise ValueError("WSB CLI timeout is invalid")

        try:
            process = self._popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(environment),
                shell=False,
                creationflags=_creation_flags(),
            )
        except OSError as error:
            raise WsbHostExecutionError("WSB CLI 启动失败，未执行沙箱命令。") from error
        if process.stdout is None or process.stderr is None:
            _terminate_process(process)
            raise WsbHostExecutionError("WSB CLI 未提供受控输出管道。")

        stdout = _BoundedCollector(stdout_limit)
        stderr = _BoundedCollector(stderr_limit)
        stdout_thread = Thread(
            target=stdout.drain,
            args=(process.stdout,),
            daemon=True,
        )
        stderr_thread = Thread(
            target=stderr.drain,
            args=(process.stderr,),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        deadline = monotonic() + timeout_seconds
        termination: Literal["timeout", "cancelled", "output"] | None = None
        try:
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    termination = "cancelled"
                    break
                if stdout.overflowed or stderr.overflowed:
                    termination = "output"
                    break
                if monotonic() >= deadline:
                    termination = "timeout"
                    break
                Event().wait(PROCESS_POLL_SECONDS)
            if termination is not None:
                _terminate_process(process)
            else:
                process.wait()
        finally:
            stdout_thread.join(PROCESS_TERMINATE_GRACE_SECONDS)
            stderr_thread.join(PROCESS_TERMINATE_GRACE_SECONDS)
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass

        if stdout_thread.is_alive() or stderr_thread.is_alive():
            raise WsbHostExecutionError("WSB CLI 输出管道未能安全收口。")
        if stdout.failed or stderr.failed:
            raise WsbHostExecutionError("WSB CLI 输出读取失败。")
        stdout_bytes = stdout.bytes()
        stderr_bytes = stderr.bytes()
        if termination == "timeout":
            return WsbCliCompleted(
                returncode=None,
                stdout=stdout_bytes,
                stderr=stderr_bytes,
                timed_out=True,
            )
        if termination == "cancelled":
            return WsbCliCompleted(
                returncode=None,
                stdout=stdout_bytes,
                stderr=stderr_bytes,
                cancelled=True,
            )
        if termination == "output" or stdout.overflowed or stderr.overflowed:
            return WsbCliCompleted(
                returncode=None,
                stdout=stdout_bytes,
                stderr=stderr_bytes,
                output_limited=True,
            )
        return WsbCliCompleted(
            returncode=process.returncode,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
        )


class WsbHostExecutor:
    """Execute the candidate WSB host sequence without exposing a general tool."""

    __slots__ = ("_environment", "_runner", "_wsb_executable")

    def __init__(
        self,
        wsb_executable: Path,
        *,
        cli_runner: WsbCliRunner | None = None,
    ) -> None:
        if not isinstance(wsb_executable, Path) or not wsb_executable.is_absolute():
            raise ValueError("wsb executable must be an absolute Path")
        if "\0" in str(wsb_executable):
            raise ValueError("wsb executable path is invalid")
        self._wsb_executable = _validate_wsb_executable(wsb_executable)
        self._runner = cli_runner or BoundedSubprocessCliRunner()
        self._environment = _minimal_environment()

    def execute(
        self,
        plan: WsbExecutionPlan,
        *,
        cancel: CancellationSignal | None = None,
    ) -> WsbExecutionResult:
        """Run the fixed host state machine or raise a sanitized sandbox error."""

        if cancel is not None and cancel.is_set():
            raise WsbHostExecutionError("Windows Sandbox 执行在启动前已取消。")
        paths = _validate_plan_paths(plan)
        request = _validate_control_bundle(plan, paths.control)
        output = _create_output_directory(paths.temporary_root)
        config = _build_start_config(paths.snapshot, paths.control)
        deadline = monotonic() + plan.timeout_seconds
        cleanup_required = False
        result: WsbExecutionResult | None = None
        primary_error: BaseException | None = None
        try:
            cleanup_required = True
            self._invoke(
                "start",
                (
                    "start",
                    "--id",
                    str(plan.instance_id),
                    "--config",
                    config,
                    "--raw",
                ),
                plan,
                deadline,
                cancel,
            )
            if _validate_control_bundle(plan, paths.control) != request:
                raise WsbHostExecutionError(
                    "guest request changed after the sandbox was started."
                )
            self._invoke(
                "runner",
                _exec_arguments(plan.instance_id, WSB_RUNNER_COMMAND),
                plan,
                deadline,
                cancel,
                require_exit_code=True,
            )

            # The trusted runner exits zero only after its complete guest Job is
            # empty.  No writable host mapping exists before this point.
            _require_empty_output(output)
            self._invoke(
                "share",
                (
                    "share",
                    "--id",
                    str(plan.instance_id),
                    "--host-path",
                    str(output),
                    "--sandbox-path",
                    WSB_GUEST_EXPORT,
                    "--allow-write",
                    "--raw",
                ),
                plan,
                deadline,
                cancel,
            )
            if _validate_control_bundle(plan, paths.control) != request:
                raise WsbHostExecutionError(
                    "guest request changed before result export."
                )
            self._invoke(
                "exporter",
                _exec_arguments(plan.instance_id, WSB_EXPORTER_COMMAND),
                plan,
                deadline,
                cancel,
                require_exit_code=True,
            )
            result = _load_result(output, plan, request)
        except BaseException as error:  # cleanup must also cover interrupts.
            primary_error = error
        finally:
            if cleanup_required:
                try:
                    self._invoke_stop(plan)
                except BaseException as stop_error:
                    raise WsbHostExecutionError(
                        "Windows Sandbox stop 未被严格确认，执行结果已拒绝。"
                    ) from stop_error

        if primary_error is not None:
            if isinstance(primary_error, WsbHostExecutionError):
                raise primary_error
            raise WsbHostExecutionError(
                "Windows Sandbox 主机执行失败，未返回结果。"
            ) from primary_error
        if result is None:
            raise WsbHostExecutionError("Windows Sandbox 未生成可信结果。")
        return result

    def _invoke(
        self,
        stage: str,
        arguments: tuple[str, ...],
        plan: WsbExecutionPlan,
        deadline: float,
        cancel: CancellationSignal | None,
        *,
        require_exit_code: bool = False,
    ) -> dict[str, object]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise WsbHostExecutionError(f"Windows Sandbox {stage} 阶段超过总超时。")
        completed = self._runner.run(
            (str(self._wsb_executable), *arguments),
            timeout_seconds=remaining,
            stdout_limit=MAX_CLI_STDOUT_BYTES,
            stderr_limit=MAX_CLI_STDERR_BYTES,
            environment=self._environment,
            cancel=cancel,
        )
        return _validate_cli_completion(
            completed,
            stage=stage,
            instance_id=plan.instance_id,
            require_exit_code=require_exit_code,
        )

    def _invoke_stop(self, plan: WsbExecutionPlan) -> None:
        completed = self._runner.run(
            (
                str(self._wsb_executable),
                "stop",
                "--id",
                str(plan.instance_id),
                "--raw",
            ),
            timeout_seconds=STOP_TIMEOUT_SECONDS,
            stdout_limit=MAX_CLI_STDOUT_BYTES,
            stderr_limit=MAX_CLI_STDERR_BYTES,
            environment=self._environment,
            cancel=None,
        )
        _validate_cli_completion(
            completed,
            stage="stop",
            instance_id=plan.instance_id,
            require_exit_code=False,
        )


@dataclass(frozen=True, slots=True)
class _ValidatedPaths:
    snapshot: Path
    control: Path
    temporary_root: Path


def _exec_arguments(instance_id: UUID, command: str) -> tuple[str, ...]:
    return (
        "exec",
        "--id",
        str(instance_id),
        "--command",
        command,
        "--run-as",
        "System",
        "--working-directory",
        WSB_GUEST_CONTROL,
        "--raw",
    )


def _validate_cli_completion(
    completed: WsbCliCompleted,
    *,
    stage: str,
    instance_id: UUID,
    require_exit_code: bool,
) -> dict[str, object]:
    allowed_statuses = _CLI_STAGE_ALLOWED_STATUSES.get(stage)
    if allowed_statuses is None:
        raise WsbHostExecutionError("Windows Sandbox 返回了未知主机执行阶段。")
    if completed.timed_out:
        raise WsbHostExecutionError(f"Windows Sandbox {stage} 阶段超时。")
    if completed.cancelled:
        raise WsbHostExecutionError(f"Windows Sandbox {stage} 阶段已取消。")
    if completed.output_limited:
        raise WsbHostExecutionError(f"Windows Sandbox {stage} 阶段输出超过上限。")
    if completed.returncode != 0:
        raise WsbHostExecutionError(f"Windows Sandbox {stage} 阶段失败。")
    if completed.stderr.strip():
        raise WsbHostExecutionError(f"Windows Sandbox {stage} 阶段产生未预期错误输出。")
    payload = _parse_bounded_json(completed.stdout, MAX_CLI_STDOUT_BYTES)
    if not isinstance(payload, dict):
        raise WsbHostExecutionError(f"Windows Sandbox {stage} 阶段未返回 JSON 对象。")
    allowed_keys = {"Id", "Success", "Status", "State"}
    if require_exit_code:
        allowed_keys.add("ExitCode")
    if not set(payload) <= allowed_keys or "Id" not in payload:
        raise WsbHostExecutionError(
            f"Windows Sandbox {stage} 阶段返回了未审计的 raw JSON schema。"
        )
    if "Status" in payload and "State" in payload:
        raise WsbHostExecutionError(f"Windows Sandbox {stage} 阶段返回了歧义状态字段。")
    response_id = payload.get("Id")
    if response_id != str(instance_id):
        raise WsbHostExecutionError(f"Windows Sandbox {stage} 阶段实例绑定不匹配。")
    success = payload.get("Success")
    if success is not None and (type(success) is not bool or success is not True):
        raise WsbHostExecutionError(f"Windows Sandbox {stage} 阶段报告失败。")
    status = payload.get("Status", payload.get("State"))
    if status is not None and (
        not isinstance(status, str) or status not in allowed_statuses
    ):
        raise WsbHostExecutionError(
            f"Windows Sandbox {stage} 阶段返回了矛盾或未审计的状态。"
        )
    if require_exit_code:
        exit_code = payload.get("ExitCode")
        if type(exit_code) is not int or exit_code != 0:
            raise WsbHostExecutionError(
                f"Windows Sandbox {stage} 阶段的可信 runner 未正常结束。"
            )
    _validate_json_shape(payload)
    return payload


def _load_result(
    output: Path,
    plan: WsbExecutionPlan,
    request: SandboxGuestRequest,
) -> WsbExecutionResult:
    _validate_directory(output, "output directory")
    entries = tuple(output.iterdir())
    if len(entries) != 1 or entries[0].name != WSB_RESULT_FILENAME:
        raise WsbHostExecutionError("输出目录必须只包含一个固定结果文件。")
    raw = _read_regular_file(entries[0], MAX_RESULT_JSON_BYTES)
    try:
        guest_result = parse_guest_result(raw, request=request)
    except SandboxGuestError as error:
        raise WsbHostExecutionError(
            "guest result was invalid or not bound to this execution."
        ) from error
    if guest_result.job_terminated is not True:
        raise WsbHostExecutionError(
            "guest result did not confirm complete Job termination."
        )
    return WsbExecutionResult(
        instance_id=plan.instance_id,
        run_id=plan.run_id,
        request_hash=plan.request_hash,
        status=guest_result.status,
        exit_code=guest_result.exit_code,
        stdout=guest_result.stdout,
        stderr=guest_result.stderr,
        duration_ms=guest_result.duration_ms,
        error_code=guest_result.error_code,
        result_hash=guest_result.result_hash,
        job_terminated=True,
    )


def _validate_plan_paths(plan: WsbExecutionPlan) -> _ValidatedPaths:
    snapshot = _validate_directory(plan.snapshot_directory, "snapshot directory")
    control = _validate_directory(plan.control_directory, "control directory")
    temporary_root = _validate_directory(plan.temporary_root, "temporary root")
    paths = (snapshot, control, temporary_root)
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if _contains_path(left, right) or _contains_path(right, left):
                raise WsbHostExecutionError("snapshot、control 与临时根必须彼此独立。")
    _validate_snapshot_tree(snapshot)
    _require_empty_directory(temporary_root, "temporary root")
    return _ValidatedPaths(snapshot, control, temporary_root)


def _validate_control_bundle(
    plan: WsbExecutionPlan,
    control: Path,
) -> SandboxGuestRequest:
    entries = {entry.name: entry for entry in control.iterdir()}
    if set(entries) != {WSB_RUNNER_FILENAME, WSB_REQUEST_FILENAME}:
        raise WsbHostExecutionError("control 目录必须只包含固定 runner 和请求。")
    runner = entries[WSB_RUNNER_FILENAME]
    runner_bytes = _read_regular_file(runner, MAX_RUNNER_BYTES)
    if not runner_bytes or sha256(runner_bytes).hexdigest() != plan.runner_sha256:
        raise WsbHostExecutionError("可信 guest runner 的 SHA-256 不匹配。")
    request_bytes = _read_regular_file(
        entries[WSB_REQUEST_FILENAME],
        MAX_REQUEST_JSON_BYTES,
    )
    try:
        request = parse_guest_request(request_bytes)
    except SandboxGuestError as error:
        raise WsbHostExecutionError(
            "guest request is invalid or non-canonical."
        ) from error
    if (
        request.instance_id != plan.instance_id.hex
        or request.run_id != plan.run_id.hex
        or request.request_hash != plan.request_hash
        or request.canonical_bytes() != request_bytes
    ):
        raise WsbHostExecutionError(
            "guest request identity or digest binding does not match the plan."
        )
    return request


def _create_output_directory(temporary_root: Path) -> Path:
    output = temporary_root / "export"
    if output.exists() or output.is_symlink():
        raise WsbHostExecutionError("专用输出目录必须由本次执行新建。")
    try:
        output.mkdir()
    except OSError as error:
        raise WsbHostExecutionError("无法新建专用输出目录。") from error
    return _validate_directory(output, "output directory")


def _require_empty_output(output: Path) -> None:
    _validate_directory(output, "output directory")
    _require_empty_directory(output, "output directory")


def _require_empty_directory(directory: Path, label: str) -> None:
    try:
        next(directory.iterdir())
    except StopIteration:
        return
    except OSError as error:
        raise WsbHostExecutionError(f"无法复核 {label}。") from error
    raise WsbHostExecutionError(f"{label} 必须为空。")


def _build_start_config(snapshot: Path, control: Path) -> str:
    root = ElementTree.Element("Configuration")
    for name, value in (
        ("VGpu", "Disable"),
        ("Networking", "Disable"),
        ("AudioInput", "Disable"),
        ("VideoInput", "Disable"),
        ("PrinterRedirection", "Disable"),
        ("ClipboardRedirection", "Disable"),
        ("ProtectedClient", "Enable"),
        ("MemoryInMB", "2048"),
    ):
        ElementTree.SubElement(root, name).text = value
    mapped = ElementTree.SubElement(root, "MappedFolders")
    for host, guest in (
        (snapshot, WSB_GUEST_SNAPSHOT),
        (control, WSB_GUEST_CONTROL),
    ):
        item = ElementTree.SubElement(mapped, "MappedFolder")
        ElementTree.SubElement(item, "HostFolder").text = str(host)
        ElementTree.SubElement(item, "SandboxFolder").text = guest
        ElementTree.SubElement(item, "ReadOnly").text = "true"
    result = ElementTree.tostring(root, encoding="unicode", short_empty_elements=False)
    if len(result) > 16_000 or "\0" in result:
        raise WsbHostExecutionError("Windows Sandbox 启动配置无效或过长。")
    return result


def _validate_snapshot_tree(root: Path) -> None:
    pending = [root]
    entries_seen = 0
    bytes_seen = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > MAX_TREE_ENTRIES:
                        raise WsbHostExecutionError("snapshot 条目数量超过上限。")
                    if _is_sensitive_name(entry.name):
                        raise WsbHostExecutionError("snapshot 包含受保护路径。")
                    metadata = entry.stat(follow_symlinks=False)
                    if entry.is_symlink() or _metadata_is_reparse(metadata):
                        raise WsbHostExecutionError("snapshot 包含重解析点。")
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(Path(entry.path))
                    elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
                        raise WsbHostExecutionError(
                            "snapshot 只能包含独立普通文件和目录。"
                        )
                    else:
                        bytes_seen += metadata.st_size
                        if bytes_seen > MAX_TREE_BYTES:
                            raise WsbHostExecutionError("snapshot 累计字节超过上限。")
        except WsbHostExecutionError:
            raise
        except OSError as error:
            raise WsbHostExecutionError("snapshot 扫描失败。") from error


def _validate_directory(path: Path, label: str) -> Path:
    if not path.is_absolute() or _path_has_reparse_component(path):
        raise WsbHostExecutionError(f"{label} 必须是无重解析点的绝对目录。")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as error:
        raise WsbHostExecutionError(f"{label} 不存在或不可验证。") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or _metadata_is_reparse(metadata)
        or resolved.parent == resolved
    ):
        raise WsbHostExecutionError(f"{label} 必须是真实非根目录。")
    return resolved


def _read_regular_file(path: Path, limit: int) -> bytes:
    if _path_has_reparse_component(path):
        raise WsbHostExecutionError("拒绝读取重解析点文件。")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink > 1
            or before.st_size > limit
        ):
            raise WsbHostExecutionError("受控文件不是有界独立普通文件。")
        chunks: list[bytes] = []
        bytes_read = 0
        while bytes_read <= limit:
            chunk = os.read(descriptor, min(65_536, limit + 1 - bytes_read))
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(data) > limit
            or not _same_file_metadata(before, after)
            or len(data) != before.st_size
        ):
            raise WsbHostExecutionError("受控文件在读取期间变化或超过上限。")
        return data
    except WsbHostExecutionError:
        raise
    except OSError as error:
        raise WsbHostExecutionError("受控文件读取失败。") from error
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _parse_bounded_json(raw: bytes, limit: int) -> object:
    if len(raw) > limit:
        raise WsbHostExecutionError("JSON 数据超过上限。")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WsbHostExecutionError("JSON 数据不是 UTF-8。") from error

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise WsbHostExecutionError("JSON 数据格式无效。") from error
    _validate_json_shape(value)
    return value


def _validate_json_shape(value: object) -> None:
    remaining = [(value, 0)]
    items = 0
    while remaining:
        current, depth = remaining.pop()
        if depth > MAX_JSON_DEPTH:
            raise WsbHostExecutionError("JSON 嵌套超过上限。")
        items += 1
        if items > MAX_JSON_ITEMS:
            raise WsbHostExecutionError("JSON 项目数量超过上限。")
        if current is None or isinstance(current, (str, bool, int)):
            continue
        if isinstance(current, float):
            raise WsbHostExecutionError("JSON 不允许浮点数。")
        if isinstance(current, list):
            remaining.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            if any(
                not isinstance(key, str)
                or not key
                or len(key) > 128
                or _contains_control(key)
                for key in current
            ):
                raise WsbHostExecutionError("JSON 对象键无效。")
            remaining.extend((item, depth + 1) for item in current.values())
            continue
        raise WsbHostExecutionError("JSON 包含不支持的值。")


def _validate_uuid4(label: str, value: UUID) -> None:
    if not isinstance(value, UUID) or value.version != 4 or value.variant != RFC_4122:
        raise ValueError(f"{label} must be a random RFC 4122 UUID4")


def _validate_digest(label: str, value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _minimal_environment() -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in ("SYSTEMROOT", "WINDIR")
        if name in os.environ
    }
    environment.update({"NO_COLOR": "1"})
    return environment


def _validate_wsb_executable(path: Path) -> Path:
    if _path_has_reparse_component(path):
        raise ValueError("wsb executable cannot contain a reparse point")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as error:
        raise ValueError("wsb executable does not exist") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or _metadata_is_reparse(metadata)
        or resolved.name.casefold() != "wsb.exe"
    ):
        raise ValueError("wsb executable must be a real Windows Sandbox CLI file")
    return resolved


def _same_file_metadata(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_mode == after.st_mode
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
    )


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.terminate()
        process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WsbHostExecutionError("WSB CLI 进程无法终止。") from error


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _contains_path(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _is_sensitive_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered in _BLOCKED_DIRECTORIES
        or lowered in _BLOCKED_FILE_NAMES
        or lowered == ".env"
        or (lowered.startswith(".env.") and lowered != ".env.example")
        or Path(lowered).suffix in _BLOCKED_SUFFIXES
    )


def _metadata_is_reparse(metadata: os.stat_result) -> bool:
    return bool(
        int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_POINT_ATTRIBUTE
    )


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or _metadata_is_reparse(metadata)


def _path_has_reparse_component(path: Path) -> bool:
    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if _is_reparse_point(current):
            return True
    return False
