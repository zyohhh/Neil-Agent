"""Certified Windows Sandbox guest protocol and fixed runner compiler.

The runner contract is labelled certified only because runtime readiness still
requires a fresh, independently reviewed, cryptographically attested evidence
bundle for this exact source and host.  Importing this module or compiling the
runner never grants readiness by itself.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from unicodedata import category

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

GUEST_PROTOCOL_VERSION: Literal[2] = 2
GUEST_RUNNER_VERSION: Literal[3] = 3
GUEST_RUNNER_SECURITY_ASSURANCE: Literal["certified-windows-sandbox-v1"] = (
    "certified-windows-sandbox-v1"
)
GUEST_SOURCE_FILENAME = "sandbox_guest_runner.cs"
GUEST_BINARY_FILENAME = "neil-sandbox-runner.exe"
GUEST_CONTROL_DIRECTORY = r"C:\NeilAgent\Control"
GUEST_SNAPSHOT_DIRECTORY = r"C:\NeilAgent\Snapshot"
GUEST_SCRATCH_DIRECTORY = r"C:\NeilAgent\Scratch"
GUEST_RESULT_DIRECTORY = r"C:\NeilAgent\Result"
GUEST_EXPORT_DIRECTORY = r"C:\NeilAgent\Export"
GUEST_REQUEST_FILENAME = "request.json"
GUEST_RESULT_FILENAME = "result.json"
GUEST_COMPLETE_MARKER_FILENAME = "complete.marker"
GUEST_EXECUTE_MODE = "execute"
GUEST_EXPORT_MODE = "export"

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_ARGUMENTS = 64
MAX_ARGUMENT_CHARS = 4_096
MAX_ENVIRONMENT_ITEMS = 16
MAX_ENVIRONMENT_VALUE_CHARS = 4_096
MAX_RELATIVE_PATH_CHARS = 240
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 120_000
MIN_OUTPUT_BYTES = 1_024
MAX_OUTPUT_BYTES = 1_000_000
MIN_MEMORY_BYTES = 16 * 1024 * 1024
MAX_PROCESS_MEMORY_BYTES = 512 * 1024 * 1024
MAX_JOB_MEMORY_BYTES = 1024 * 1024 * 1024
MAX_ACTIVE_PROCESSES = 16
MAX_COMPILE_OUTPUT_BYTES = 64 * 1024

DOTNET_FRAMEWORK_CSC_PATHS = (
    Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"),
)

_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_ENVIRONMENT_NAME = re.compile(r"^NEIL_[A-Z0-9_]{1,58}$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}

GuestStatus = Literal[
    "exited",
    "timeout",
    "cancelled",
    "output_limit",
    "resource_limit",
    "runner_error",
]
GuestErrorCode = Literal[
    "timeout",
    "cancelled",
    "output_limit",
    "resource_limit",
    "create_process",
    "job_setup",
    "runner_failure",
]
CompilerRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class SandboxGuestError(RuntimeError):
    """Fail-closed guest protocol or runner build error."""


class SandboxGuestRequest(BaseModel):
    """Canonical immutable execution request consumed by the fixed guest runner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[2] = GUEST_PROTOCOL_VERSION
    run_id: StrictStr
    request_hash: StrictStr
    instance_id: StrictStr
    snapshot_manifest_sha256: StrictStr
    runner_source_sha256: StrictStr
    approval_binding_version: Literal[1] = 1
    approval_binding_sha256: StrictStr
    executable: StrictStr
    argv: tuple[StrictStr, ...] = Field(max_length=MAX_ARGUMENTS)
    cwd: StrictStr = "."
    environment: dict[StrictStr, StrictStr] = Field(
        default_factory=dict,
        max_length=MAX_ENVIRONMENT_ITEMS,
    )
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

    @field_validator("run_id", "instance_id")
    @classmethod
    def identifiers_are_canonical(cls, value: str) -> str:
        if not _HEX_32.fullmatch(value):
            raise ValueError("guest identifiers must be 32 lowercase hex characters")
        return value

    @field_validator(
        "request_hash",
        "snapshot_manifest_sha256",
        "runner_source_sha256",
        "approval_binding_sha256",
    )
    @classmethod
    def hashes_are_canonical(cls, value: str) -> str:
        if not _HEX_64.fullmatch(value):
            raise ValueError("guest hashes must be 64 lowercase hex characters")
        return value

    @field_validator("executable")
    @classmethod
    def executable_is_safe_relative_path(cls, value: str) -> str:
        _validate_relative_windows_path(value, allow_dot=False)
        if not value.lower().endswith(".exe"):
            raise ValueError("guest executable must have an explicit .exe suffix")
        return value

    @field_validator("cwd")
    @classmethod
    def cwd_is_safe_relative_path(cls, value: str) -> str:
        _validate_relative_windows_path(value, allow_dot=True)
        return value

    @field_validator("argv")
    @classmethod
    def arguments_are_bounded(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        for argument in value:
            _validate_untrusted_text(
                argument,
                max_chars=MAX_ARGUMENT_CHARS,
                label="guest argument",
            )
        return value

    @field_validator("environment")
    @classmethod
    def environment_is_minimal(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for name, item in value.items():
            if not _ENVIRONMENT_NAME.fullmatch(name):
                raise ValueError("guest environment names must use the NEIL_ prefix")
            _validate_untrusted_text(
                item,
                max_chars=MAX_ENVIRONMENT_VALUE_CHARS,
                label="guest environment value",
            )
            normalized[name] = item
        return normalized

    @model_validator(mode="after")
    def limits_and_hash_are_bound(self) -> SandboxGuestRequest:
        if self.job_memory_bytes < self.process_memory_bytes:
            raise ValueError("job memory must be at least process memory")
        if self.request_hash != _digest_json(self.hash_payload()):
            raise ValueError("request hash does not match canonical request payload")
        return self

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        instance_id: str,
        snapshot_manifest_sha256: str,
        runner_source_sha256: str,
        approval_binding_sha256: str,
        executable: str,
        argv: Sequence[str] = (),
        cwd: str = ".",
        environment: Mapping[str, str] | None = None,
        timeout_ms: int = 30_000,
        max_output_bytes: int = 256_000,
        active_process_limit: int = 4,
        process_memory_bytes: int = 256 * 1024 * 1024,
        job_memory_bytes: int = 512 * 1024 * 1024,
    ) -> SandboxGuestRequest:
        """Build and hash one request without accepting caller-provided hashes."""

        payload: dict[str, object] = {
            "version": GUEST_PROTOCOL_VERSION,
            "run_id": run_id,
            "instance_id": instance_id,
            "snapshot_manifest_sha256": snapshot_manifest_sha256,
            "runner_source_sha256": runner_source_sha256,
            "approval_binding_version": 1,
            "approval_binding_sha256": approval_binding_sha256,
            "executable": executable,
            "argv": list(argv),
            "cwd": cwd,
            "environment": dict(environment or {}),
            "timeout_ms": timeout_ms,
            "max_output_bytes": max_output_bytes,
            "active_process_limit": active_process_limit,
            "process_memory_bytes": process_memory_bytes,
            "job_memory_bytes": job_memory_bytes,
        }
        return cls.model_validate(
            {
                **payload,
                "request_hash": _digest_json(payload),
            }
        )

    def hash_payload(self) -> dict[str, object]:
        """Return the exact request object covered by ``request_hash``."""

        return self.model_dump(mode="json", exclude={"request_hash"})

    def canonical_bytes(self) -> bytes:
        """Return the only accepted on-disk request encoding."""

        if self.request_hash != _digest_json(self.hash_payload()):
            raise SandboxGuestError("guest request changed after validation")
        return _canonical_json_bytes(self.model_dump(mode="json"))


class SandboxGuestResult(BaseModel):
    """Canonical bounded result emitted by the fixed guest runner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[2] = GUEST_PROTOCOL_VERSION
    runner_version: Literal[3] = GUEST_RUNNER_VERSION
    security_assurance: Literal["certified-windows-sandbox-v1"] = (
        GUEST_RUNNER_SECURITY_ASSURANCE
    )
    run_id: StrictStr
    request_hash: StrictStr
    instance_id: StrictStr
    status: GuestStatus
    exit_code: StrictInt | None
    stdout_b64: StrictStr = Field(max_length=(MAX_OUTPUT_BYTES * 4 // 3) + 8)
    stderr_b64: StrictStr = Field(max_length=(MAX_OUTPUT_BYTES * 4 // 3) + 8)
    stdout_bytes: StrictInt = Field(ge=0, le=MAX_OUTPUT_BYTES)
    stderr_bytes: StrictInt = Field(ge=0, le=MAX_OUTPUT_BYTES)
    duration_ms: StrictInt = Field(ge=0, le=MAX_TIMEOUT_MS + 10_000)
    error_code: GuestErrorCode | None
    job_terminated: StrictBool
    result_hash: StrictStr

    @field_validator("run_id", "instance_id")
    @classmethod
    def identifiers_are_canonical(cls, value: str) -> str:
        if not _HEX_32.fullmatch(value):
            raise ValueError("guest identifiers must be 32 lowercase hex characters")
        return value

    @field_validator("request_hash", "result_hash")
    @classmethod
    def hashes_are_canonical(cls, value: str) -> str:
        if not _HEX_64.fullmatch(value):
            raise ValueError("guest hashes must be 64 lowercase hex characters")
        return value

    @model_validator(mode="after")
    def output_status_and_hash_are_valid(self) -> SandboxGuestResult:
        stdout = _decode_canonical_base64(self.stdout_b64)
        stderr = _decode_canonical_base64(self.stderr_b64)
        if len(stdout) != self.stdout_bytes or len(stderr) != self.stderr_bytes:
            raise ValueError("guest output lengths do not match encoded output")
        if self.stdout_bytes + self.stderr_bytes > MAX_OUTPUT_BYTES:
            raise ValueError("combined guest output exceeds the protocol maximum")
        if self.status == "exited":
            if self.exit_code is None or self.error_code is not None:
                raise ValueError("exited results require an exit code and no error")
        elif self.status in {
            "timeout",
            "cancelled",
            "output_limit",
            "resource_limit",
        }:
            if self.error_code != self.status:
                raise ValueError("execution failure status and error code must match")
        elif self.error_code not in {
            "create_process",
            "job_setup",
            "runner_failure",
        }:
            raise ValueError("runner errors require a stable runner error code")
        if not self.job_terminated:
            raise ValueError("guest result did not confirm an empty terminated job")
        if self.result_hash != _digest_json(self.hash_payload()):
            raise ValueError("result hash does not match canonical result payload")
        return self

    def hash_payload(self) -> dict[str, object]:
        """Return the exact result object covered by ``result_hash``."""

        return self.model_dump(mode="json", exclude={"result_hash"})

    def canonical_bytes(self) -> bytes:
        """Return the only accepted exported result encoding."""

        if self.result_hash != _digest_json(self.hash_payload()):
            raise SandboxGuestError("guest result changed after validation")
        return _canonical_json_bytes(self.model_dump(mode="json"))

    @property
    def stdout(self) -> bytes:
        return _decode_canonical_base64(self.stdout_b64)

    @property
    def stderr(self) -> bytes:
        return _decode_canonical_base64(self.stderr_b64)


def parse_guest_request(payload: bytes) -> SandboxGuestRequest:
    """Strictly parse one canonical, duplicate-free guest request."""

    value = _decode_canonical_json(payload, max_bytes=MAX_REQUEST_BYTES)
    try:
        request = SandboxGuestRequest.model_validate(value)
    except ValueError as error:
        raise SandboxGuestError("guest request is invalid") from error
    if request.canonical_bytes() != payload:
        raise SandboxGuestError("guest request is not canonical JSON")
    return request


def parse_guest_result(
    payload: bytes,
    *,
    request: SandboxGuestRequest,
) -> SandboxGuestResult:
    """Parse and bind one exported result to its exact request and instance."""

    value = _decode_canonical_json(payload, max_bytes=MAX_RESULT_BYTES)
    try:
        result = SandboxGuestResult.model_validate(value)
    except ValueError as error:
        raise SandboxGuestError("guest result is invalid") from error
    if result.canonical_bytes() != payload:
        raise SandboxGuestError("guest result is not canonical JSON")
    if (
        result.run_id != request.run_id
        or result.request_hash != request.request_hash
        or result.instance_id != request.instance_id
    ):
        raise SandboxGuestError("guest result is not bound to this request")
    if result.stdout_bytes + result.stderr_bytes > request.max_output_bytes:
        raise SandboxGuestError("guest result exceeds its requested output limit")
    if result.duration_ms > request.timeout_ms + 10_000:
        raise SandboxGuestError("guest result exceeds its timeout envelope")
    return result


@dataclass(frozen=True, slots=True)
class GuestRunnerBuild:
    """Hashes and paths for one ephemeral fixed-runner compilation."""

    compiler_path: Path
    source_path: Path
    binary_path: Path
    source_sha256: str
    binary_sha256: str


def find_dotnet_framework_csc() -> Path | None:
    """Return the first safe compiler from the fixed .NET Framework locations."""

    for candidate in DOTNET_FRAMEWORK_CSC_PATHS:
        try:
            _require_safe_regular_file(
                candidate,
                "C# compiler",
                allow_hardlinks=True,
            )
            _require_safe_regular_file(
                candidate.parent / "System.Web.Extensions.dll",
                "C# reference",
                allow_hardlinks=True,
            )
        except SandboxGuestError:
            continue
        return candidate
    return None


@contextmanager
def build_guest_runner(
    *,
    _compiler_runner: CompilerRunner = subprocess.run,
) -> Iterator[GuestRunnerBuild]:
    """Compile the fixed source in a dedicated temporary directory."""

    compiler = find_dotnet_framework_csc()
    if compiler is None:
        raise SandboxGuestError("supported .NET Framework C# compiler is unavailable")
    source = Path(__file__).with_name(GUEST_SOURCE_FILENAME)
    source_bytes = _read_safe_regular_file(source, "guest runner source")
    if len(source_bytes) > 512 * 1024:
        raise SandboxGuestError("guest runner source exceeds its fixed size limit")
    source_hash = sha256(source_bytes).hexdigest()
    reference = compiler.parent / "System.Web.Extensions.dll"

    with TemporaryDirectory(prefix="neil-agent-guest-runner-") as temporary:
        build_root = Path(temporary).resolve()
        build_source = build_root / GUEST_SOURCE_FILENAME
        binary = build_root / GUEST_BINARY_FILENAME
        _write_exclusive(build_source, source_bytes)
        argv = [
            str(compiler),
            "/nologo",
            "/target:exe",
            "/optimize+",
            "/checked+",
            "/warnaserror+",
            "/debug-",
            "/platform:anycpu",
            f"/reference:{reference}",
            f"/out:{binary}",
            str(build_source),
        ]
        environment = {
            "SystemRoot": r"C:\Windows",
            "WINDIR": r"C:\Windows",
            "TEMP": str(build_root),
            "TMP": str(build_root),
        }
        try:
            completed = _compiler_runner(
                argv,
                shell=False,
                cwd=build_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SandboxGuestError("guest runner compilation failed closed") from error
        if completed.returncode != 0:
            raise SandboxGuestError(
                f"guest runner compiler returned exit code {completed.returncode}"
            )
        if (
            len(completed.stdout) > MAX_COMPILE_OUTPUT_BYTES
            or len(completed.stderr) > MAX_COMPILE_OUTPUT_BYTES
        ):
            raise SandboxGuestError("guest runner compiler output exceeded its limit")
        binary_bytes = _read_safe_regular_file(binary, "compiled guest runner")
        if not binary_bytes:
            raise SandboxGuestError("compiled guest runner is empty")
        yield GuestRunnerBuild(
            compiler_path=compiler,
            source_path=build_source,
            binary_path=binary,
            source_sha256=source_hash,
            binary_sha256=sha256(binary_bytes).hexdigest(),
        )


def _canonical_json_bytes(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise SandboxGuestError("guest protocol value is not canonical JSON") from error
    return text.encode("utf-8")


def _digest_json(value: object) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _decode_canonical_json(payload: bytes, *, max_bytes: int) -> dict[str, object]:
    if not payload or len(payload) > max_bytes:
        raise SandboxGuestError("guest protocol payload has an invalid size")
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise SandboxGuestError("guest protocol payload is not strict JSON") from error
    if not isinstance(value, dict):
        raise SandboxGuestError("guest protocol root must be an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _validate_relative_windows_path(value: str, *, allow_dot: bool) -> None:
    if value == "." and allow_dot:
        return
    _validate_untrusted_text(
        value,
        max_chars=MAX_RELATIVE_PATH_CHARS,
        label="guest path",
    )
    if (
        not value
        or "/" in value
        or ":" in value
        or value.startswith("\\")
        or value.endswith("\\")
    ):
        raise ValueError("guest paths must be canonical relative Windows paths")
    parts = value.split("\\")
    for part in parts:
        if not part or part in {".", ".."} or part.endswith((" ", ".")):
            raise ValueError("guest path contains an unsafe component")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise ValueError("guest path uses a reserved Windows device name")


def _validate_untrusted_text(value: str, *, max_chars: int, label: str) -> None:
    if len(value) > max_chars:
        raise ValueError(f"{label} exceeds its length limit")
    if any(category(character).startswith("C") for character in value):
        raise ValueError(f"{label} contains control or format characters")


def _decode_canonical_base64(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("guest output is not valid base64") from error
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("guest output base64 is not canonical")
    return decoded


def _require_safe_regular_file(
    path: Path,
    label: str,
    *,
    allow_hardlinks: bool = False,
) -> os.stat_result:
    try:
        file_stat = path.lstat()
    except OSError as error:
        raise SandboxGuestError(f"{label} is unavailable") from error
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(file_stat, "st_file_attributes", 0)
    if (
        path.is_symlink()
        or not stat.S_ISREG(file_stat.st_mode)
        or file_attributes & reparse_flag
        or (not allow_hardlinks and file_stat.st_nlink != 1)
    ):
        requirement = (
            "a real, non-reparse regular file"
            if allow_hardlinks
            else "one real, non-linked file"
        )
        raise SandboxGuestError(f"{label} must be {requirement}")
    return file_stat


def _read_safe_regular_file(path: Path, label: str) -> bytes:
    expected = _require_safe_regular_file(path, label)
    descriptor = -1
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise SandboxGuestError(f"{label} changed while being opened")
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            return source.read()
    except SandboxGuestError:
        raise
    except OSError as error:
        raise SandboxGuestError(f"{label} could not be read") from error
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = -1
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except OSError as error:
        raise SandboxGuestError("guest runner build file creation failed") from error
    finally:
        if descriptor != -1:
            os.close(descriptor)
