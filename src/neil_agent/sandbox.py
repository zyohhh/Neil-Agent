"""Fail-closed contracts and certified OS-isolated command execution.

The backend remains inert unless an independently pinned evidence bundle is
fully replayed and bound to the current source and Windows host. Tool exposure
is handled separately and only after :attr:`SandboxCapabilities.ready`.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import stat
import ctypes
from ctypes import wintypes
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4
from xml.etree import ElementTree

from .errors import SandboxError
from .sensitive_paths import is_sensitive_entry_name, is_sensitive_file_name

if TYPE_CHECKING:
    from .sandbox_approval import RunCommandApprovalBinding
    from .sandbox_snapshot import SnapshotManifest
    from .windows_sandbox import WsbExecutionPlan, WsbExecutionResult

WorkspaceMode = Literal["none", "read-only-snapshot"]
NetworkMode = Literal["deny"]
TerminationReason = Literal[
    "succeeded",
    "exit_nonzero",
    "timed_out",
    "cancelled",
    "output_limit",
    "resource_limit",
    "backend_error",
]

WINDOWS_SANDBOX_BACKEND = "windows-sandbox"
WINDOWS_SANDBOX_GUI_EXECUTABLE = "WindowsSandbox.exe"
WINDOWS_SANDBOX_CLI_EXECUTABLE = "wsb.exe"
WINDOWS_SANDBOX_GUEST_WORKSPACE = r"C:\NeilAgent\Workspace"

MIN_TIMEOUT_SECONDS = 0.1
MAX_TIMEOUT_SECONDS = 3_600.0
MIN_OUTPUT_BYTES = 1_024
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
MIN_MEMORY_BYTES = 64 * 1024 * 1024
MAX_MEMORY_BYTES = 8 * 1024 * 1024 * 1024
WINDOWS_SANDBOX_MIN_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
MAX_PROCESSES = 64
MAX_ARGUMENTS = 128
MAX_ARGUMENT_CHARS = 4_096
MAX_COMMAND_LINE_CHARS = 32_767
MAX_ENVIRONMENT_VALUE_CHARS = 1_024
MAX_SNAPSHOT_ENTRIES = 100_000
MAX_SNAPSHOT_FILE_BYTES = 50 * 1024 * 1024
MAX_SNAPSHOT_TOTAL_BYTES = 512 * 1024 * 1024

SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PYTHONUTF8",
        "TZ",
    }
)
_SECRET_ENVIRONMENT_FRAGMENTS = (
    "API_KEY",
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE",
    "SECRET",
    "TOKEN",
)
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_LOCALE_VALUE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_TIME_ZONE_VALUE = re.compile(r"^[A-Za-z0-9_+./-]{1,128}$")
_BACKEND_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_GIT_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CERTIFICATION_GATE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_BLOCKED_EXECUTABLE_SUFFIXES = frozenset(
    {".bat", ".cmd", ".js", ".jse", ".ps1", ".vbs", ".vbe", ".wsf", ".wsh"}
)
_REPARSE_POINT_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_WINDOWS_FILE_READ_ATTRIBUTES = 0x0080
_WINDOWS_FILE_SHARE_READ = 0x0001
_WINDOWS_FILE_SHARE_WRITE = 0x0002
_WINDOWS_FILE_SHARE_DELETE = 0x0004
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_INVALID_HANDLE = ctypes.c_void_p(-1).value


class CancellationSignal(Protocol):
    """Minimal cooperative cancellation signal accepted by sandbox backends."""

    def is_set(self) -> bool:
        """Return whether the caller requests termination."""


@dataclass(frozen=True, slots=True)
class _WindowsFileInformation:
    file_attributes: int
    link_count: int


class _WindowsFileApi(Protocol):
    def open_metadata(self, path: Path) -> object:
        """Open one path without following a final reparse point."""

    def query(self, handle: object) -> _WindowsFileInformation:
        """Return kernel metadata for an open file handle."""

    def close(self, handle: object) -> None:
        """Close an open file handle or raise OSError."""


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class _CtypesWindowsFileApi:
    __slots__ = ("_close_handle", "_create_file", "_get_information")

    def __init__(self) -> None:
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise OSError("Win32 APIs are unavailable")
        kernel32 = win_dll("kernel32", use_last_error=True)
        self._create_file = kernel32.CreateFileW
        self._create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._create_file.restype = wintypes.HANDLE
        self._get_information = kernel32.GetFileInformationByHandle
        self._get_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self._get_information.restype = wintypes.BOOL
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL

    def open_metadata(self, path: Path) -> object:
        handle = self._create_file(
            str(path),
            _WINDOWS_FILE_READ_ATTRIBUTES,
            (
                _WINDOWS_FILE_SHARE_READ
                | _WINDOWS_FILE_SHARE_WRITE
                | _WINDOWS_FILE_SHARE_DELETE
            ),
            None,
            _WINDOWS_OPEN_EXISTING,
            _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle in {None, _WINDOWS_INVALID_HANDLE}:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        return handle

    def query(self, handle: object) -> _WindowsFileInformation:
        information = _ByHandleFileInformation()
        if not self._get_information(handle, ctypes.byref(information)):
            raise OSError(
                ctypes.get_last_error(),
                "GetFileInformationByHandle failed",
            )
        return _WindowsFileInformation(
            file_attributes=int(information.dwFileAttributes),
            link_count=int(information.nNumberOfLinks),
        )

    def close(self, handle: object) -> None:
        if not self._close_handle(handle):
            raise OSError(ctypes.get_last_error(), "CloseHandle failed")


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Hard limits a backend must enforce before it may report readiness."""

    timeout_seconds: float = 120.0
    max_output_bytes: int = 20_000
    max_memory_bytes: int = WINDOWS_SANDBOX_MIN_MEMORY_BYTES
    max_processes: int = 8

    def __post_init__(self) -> None:
        timeout = self.timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("sandbox timeout must be a finite number")
        if not math.isfinite(timeout):
            raise ValueError("sandbox timeout must be a finite number")
        if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError(
                "sandbox timeout must be between "
                f"{MIN_TIMEOUT_SECONDS:g} and {MAX_TIMEOUT_SECONDS:g} seconds"
            )
        _validate_bounded_integer(
            "sandbox output limit",
            self.max_output_bytes,
            minimum=MIN_OUTPUT_BYTES,
            maximum=MAX_OUTPUT_BYTES,
        )
        _validate_bounded_integer(
            "sandbox memory limit",
            self.max_memory_bytes,
            minimum=MIN_MEMORY_BYTES,
            maximum=MAX_MEMORY_BYTES,
        )
        _validate_bounded_integer(
            "sandbox process limit",
            self.max_processes,
            minimum=1,
            maximum=MAX_PROCESSES,
        )


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Platform-neutral access policy for one isolated run."""

    workspace_mode: WorkspaceMode = "none"
    network: NetworkMode = "deny"
    environment: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.workspace_mode not in {"none", "read-only-snapshot"}:
            raise ValueError("workspace mode must be none or read-only-snapshot")
        if self.network != "deny":
            raise ValueError("sandbox network policy must be deny")
        if not isinstance(self.environment, tuple):
            raise ValueError("sandbox environment must be an immutable tuple")

        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in self.environment:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
            ):
                raise ValueError(
                    "sandbox environment entries must be (name, value) tuples"
                )
            raw_name, value = item
            name = raw_name.upper()
            if not _ENVIRONMENT_NAME.fullmatch(name):
                raise ValueError("sandbox environment variable name is invalid")
            if any(fragment in name for fragment in _SECRET_ENVIRONMENT_FRAGMENTS):
                raise ValueError("secret-bearing environment variables are forbidden")
            if name not in SAFE_ENVIRONMENT_NAMES:
                raise ValueError(f"sandbox environment variable is not allowed: {name}")
            if name in seen:
                raise ValueError(f"duplicate sandbox environment variable: {name}")
            if len(value) > MAX_ENVIRONMENT_VALUE_CHARS or _contains_control_character(
                value
            ):
                raise ValueError("sandbox environment value is invalid or too long")
            _validate_environment_value(name, value)
            seen.add(name)
            normalized.append((name, value))
        object.__setattr__(self, "environment", tuple(sorted(normalized)))


@dataclass(frozen=True, slots=True)
class RunSpec:
    """A shell-free executable invocation and its complete sandbox boundary."""

    executable: Path
    argv: tuple[str, ...] = ()
    policy: SandboxPolicy = field(default_factory=SandboxPolicy)
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    workspace_snapshot: Path | None = None
    working_directory: str = "."
    export_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.executable, Path):
            raise ValueError("sandbox executable must be a Path")
        executable_text = str(self.executable)
        if (
            not self.executable.is_absolute()
            or not self.executable.name
            or "\0" in executable_text
        ):
            raise ValueError("sandbox executable must be an absolute file path")
        if self.executable.suffix.lower() in _BLOCKED_EXECUTABLE_SUFFIXES:
            raise ValueError("scripts requiring a shell or interpreter are forbidden")
        if is_sensitive_file_name(self.executable.name):
            raise ValueError("sandbox executable path is sensitive")

        if not isinstance(self.argv, tuple):
            raise ValueError("sandbox argv must be an immutable tuple")
        if len(self.argv) > MAX_ARGUMENTS:
            raise ValueError(f"sandbox argv cannot exceed {MAX_ARGUMENTS} entries")
        command_chars = len(executable_text)
        for argument in self.argv:
            if not isinstance(argument, str):
                raise ValueError("sandbox argv entries must be strings")
            if (
                len(argument) > MAX_ARGUMENT_CHARS
                or "\0" in argument
                or _contains_line_separator(argument)
            ):
                raise ValueError("sandbox argument is invalid or too long")
            command_chars += len(argument) + 1
        if command_chars > MAX_COMMAND_LINE_CHARS:
            raise ValueError("sandbox command line exceeds the Windows limit")

        if not isinstance(self.policy, SandboxPolicy):
            raise ValueError("sandbox policy must be SandboxPolicy")
        if not isinstance(self.limits, SandboxLimits):
            raise ValueError("sandbox limits must be SandboxLimits")

        snapshot = self.workspace_snapshot
        if self.policy.workspace_mode == "none":
            if snapshot is not None:
                raise ValueError("workspace snapshot is forbidden in none mode")
        elif not isinstance(snapshot, Path) or not snapshot.is_absolute():
            raise ValueError(
                "read-only-snapshot mode requires an absolute workspace snapshot"
            )

        normalized_working_directory = _normalize_relative_directory(
            self.working_directory
        )
        if self.policy.workspace_mode == "none" and normalized_working_directory != ".":
            raise ValueError("a working directory requires a workspace snapshot")
        object.__setattr__(self, "working_directory", normalized_working_directory)
        if not isinstance(self.export_paths, tuple):
            raise ValueError("sandbox export paths must be an immutable tuple")
        try:
            from .sandbox_export import GuestExportError
            from .sandbox_export_collect import normalize_export_paths

            object.__setattr__(
                self,
                "export_paths",
                normalize_export_paths(list(self.export_paths)),
            )
        except GuestExportError as error:
            raise ValueError("sandbox export paths are invalid") from error


@dataclass(frozen=True, slots=True)
class SandboxCertification:
    """Runtime reference to one externally verified sandbox evidence bundle.

    This value validates shape only; constructing it is not evidence
    verification. Production code creates it only inside the runtime verifier
    after checking expiry, review trust, provenance, current-host identity,
    source bindings, and the fixed gate set.
    """

    backend: str
    git_commit_sha: str
    evidence_sha256: str
    provenance_sha256: str
    independent_review_sha256: str
    certification_sha256: str
    executable_sha256: str
    runner_source_sha256: str
    runner_binary_sha256: str
    policy_version: int
    protocol_version: int
    required_gate_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.backend, str) or not _BACKEND_IDENTIFIER.fullmatch(
            self.backend
        ):
            raise ValueError("sandbox certification backend identifier is invalid")
        if not isinstance(self.git_commit_sha, str) or not _GIT_COMMIT_SHA.fullmatch(
            self.git_commit_sha
        ):
            raise ValueError("sandbox certification Git commit SHA is invalid")
        for label, value in (
            ("evidence", self.evidence_sha256),
            ("provenance", self.provenance_sha256),
            ("independent review", self.independent_review_sha256),
            ("certification", self.certification_sha256),
            ("executable", self.executable_sha256),
            ("runner source", self.runner_source_sha256),
            ("runner binary", self.runner_binary_sha256),
        ):
            if not isinstance(value, str) or not _SHA256_DIGEST.fullmatch(value):
                raise ValueError(f"sandbox certification {label} SHA-256 is invalid")
        _validate_bounded_integer(
            "sandbox certification policy version",
            self.policy_version,
            minimum=1,
            maximum=2_147_483_647,
        )
        _validate_bounded_integer(
            "sandbox certification protocol version",
            self.protocol_version,
            minimum=1,
            maximum=2_147_483_647,
        )
        if (
            not isinstance(self.required_gate_ids, tuple)
            or not self.required_gate_ids
            or len(self.required_gate_ids) > 64
        ):
            raise ValueError(
                "sandbox certification gate IDs must be a non-empty bounded tuple"
            )
        if any(
            not isinstance(gate_id, str)
            or len(gate_id) > 128
            or not _CERTIFICATION_GATE_ID.fullmatch(gate_id)
            for gate_id in self.required_gate_ids
        ):
            raise ValueError("sandbox certification contains an invalid gate ID")
        if self.required_gate_ids != tuple(sorted(set(self.required_gate_ids))):
            raise ValueError(
                "sandbox certification gate IDs must be unique and canonical"
            )


@dataclass(frozen=True, slots=True)
class SandboxCapabilities:
    """Read-only capability report whose readiness is evidence-derived."""

    backend: str
    available: bool
    reason_code: str
    summary: str
    executable: Path | None = None
    certification: SandboxCertification | None = None
    workspace_modes: tuple[WorkspaceMode, ...] = ()
    network_modes: tuple[NetworkMode, ...] = ()
    supports_cancellation: bool = False
    supports_timeout: bool = False
    supports_output_limit: bool = False
    supports_memory_limit: bool = False
    supports_process_limit: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.backend, str) or not _BACKEND_IDENTIFIER.fullmatch(
            self.backend
        ):
            raise ValueError("sandbox backend identifier cannot be blank")
        if type(self.available) is not bool:
            raise ValueError("sandbox availability must be a boolean")
        if (
            not isinstance(self.reason_code, str)
            or not self.reason_code.strip()
            or not isinstance(self.summary, str)
            or not self.summary.strip()
        ):
            raise ValueError("sandbox capability reason and summary cannot be blank")
        if self.executable is not None and not self.executable.is_absolute():
            raise ValueError("sandbox capability executable must be absolute")
        if not self.available and self.executable is not None:
            raise ValueError("an unavailable sandbox cannot expose an executable")
        if (
            self.backend == WINDOWS_SANDBOX_BACKEND
            and self.executable is not None
            and self.executable.name.casefold()
            != WINDOWS_SANDBOX_CLI_EXECUTABLE.casefold()
        ):
            raise ValueError(
                "Windows Sandbox execution candidates must be the wsb.exe CLI"
            )
        if self.certification is not None:
            if not isinstance(self.certification, SandboxCertification):
                raise ValueError("sandbox certification must be a SandboxCertification")
            if not self.available:
                raise ValueError("an unavailable sandbox cannot be certified")
            if self.executable is None:
                raise ValueError(
                    "a certified sandbox must expose its executable candidate"
                )
            if self.certification.backend != self.backend:
                raise ValueError(
                    "sandbox certification backend does not match capabilities"
                )
        if not isinstance(self.workspace_modes, tuple):
            raise ValueError("sandbox workspace modes must be an immutable tuple")
        if len(set(self.workspace_modes)) != len(self.workspace_modes):
            raise ValueError("sandbox workspace modes must be unique")
        if any(
            mode not in {"none", "read-only-snapshot"} for mode in self.workspace_modes
        ):
            raise ValueError("sandbox capability contains an unknown workspace mode")
        if not isinstance(self.network_modes, tuple):
            raise ValueError("sandbox network modes must be an immutable tuple")
        if len(set(self.network_modes)) != len(self.network_modes):
            raise ValueError("sandbox network modes must be unique")
        if any(mode != "deny" for mode in self.network_modes):
            raise ValueError("sandbox capability contains an unsafe network mode")
        for label, value in (
            ("cancellation", self.supports_cancellation),
            ("timeout", self.supports_timeout),
            ("output limit", self.supports_output_limit),
            ("memory limit", self.supports_memory_limit),
            ("process limit", self.supports_process_limit),
        ):
            if type(value) is not bool:
                raise ValueError(f"sandbox {label} support must be a boolean")
        if not self.available and (
            self.workspace_modes
            or self.network_modes
            or self.supports_cancellation
            or self.supports_timeout
            or self.supports_output_limit
            or self.supports_memory_limit
            or self.supports_process_limit
        ):
            raise ValueError(
                "an unavailable sandbox cannot expose enforcement capabilities"
            )
        if self.reason_code == "certification_required" and (
            not self.available or self.certification is not None
        ):
            raise ValueError(
                "certification_required contradicts sandbox certification state"
            )
        if self.reason_code == "ready" and not self.ready:
            raise ValueError(
                "ready reason requires availability, certification, and all gates"
            )
        if self.ready and self.reason_code != "ready":
            raise ValueError("a ready sandbox must use the ready reason code")

    @property
    def capability_gates_complete(self) -> bool:
        """Return whether every mandatory local enforcement gate is present."""

        return (
            "read-only-snapshot" in self.workspace_modes
            and "deny" in self.network_modes
            and self.supports_cancellation
            and self.supports_timeout
            and self.supports_output_limit
            and self.supports_memory_limit
            and self.supports_process_limit
        )

    @property
    def ready(self) -> bool:
        """Derive readiness; callers cannot supply or override this value."""

        return (
            self.available
            and self.certification is not None
            and self.capability_gates_complete
        )


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Bounded terminal result returned only by a fully enforcing backend."""

    backend: str
    termination_reason: TerminationReason
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    elapsed_seconds: float = 0.0
    run_id: str | None = None
    request_hash: str | None = None
    certification_sha256: str | None = None
    exported_files: tuple[tuple[str, bytes], ...] = ()

    def __post_init__(self) -> None:
        if not self.backend.strip():
            raise ValueError("sandbox result backend cannot be blank")
        reasons = {
            "succeeded",
            "exit_nonzero",
            "timed_out",
            "cancelled",
            "output_limit",
            "resource_limit",
            "backend_error",
        }
        if self.termination_reason not in reasons:
            raise ValueError("unknown sandbox termination reason")
        if self.termination_reason == "succeeded" and self.exit_code != 0:
            raise ValueError("a successful sandbox result requires exit code zero")
        if self.termination_reason == "exit_nonzero" and (
            isinstance(self.exit_code, bool)
            or not isinstance(self.exit_code, int)
            or self.exit_code == 0
        ):
            raise ValueError("exit_nonzero requires a non-zero integer exit code")
        if self.termination_reason not in {"succeeded", "exit_nonzero"} and (
            self.exit_code is not None
        ):
            raise ValueError("terminated sandbox runs cannot report an exit code")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise ValueError("sandbox output must be text")
        output_bytes = len(self.stdout.encode("utf-8")) + len(
            self.stderr.encode("utf-8")
        )
        if output_bytes > MAX_OUTPUT_BYTES:
            raise ValueError("sandbox result exceeds the global output boundary")
        if (
            isinstance(self.elapsed_seconds, bool)
            or not isinstance(self.elapsed_seconds, (int, float))
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise ValueError("sandbox elapsed time must be a non-negative number")


class SandboxBackend(Protocol):
    """Interface implemented by fail-closed platform backends."""

    def probe(self) -> SandboxCapabilities:
        """Inspect static local capabilities without changing machine state."""

    def run(
        self,
        spec: RunSpec,
        *,
        cancel: CancellationSignal | None = None,
    ) -> SandboxResult:
        """Run with every declared boundary enforced, or raise SandboxError."""


ExecutableLocator = Callable[[str], str | None]


class RuntimeCertificationMaterial(Protocol):
    @property
    def certification(self) -> SandboxCertification: ...

    @property
    def runner_binary_path(self) -> Path: ...

    @property
    def expires_at(self) -> datetime: ...


RuntimeCertificationLoader = Callable[[Path], RuntimeCertificationMaterial]


class HostExecutor(Protocol):
    def execute(
        self,
        plan: WsbExecutionPlan,
        *,
        cancel: CancellationSignal | None = None,
    ) -> WsbExecutionResult: ...


HostExecutorFactory = Callable[[Path], HostExecutor]


class WindowsSandboxBackend:
    """Certified Windows Sandbox backend with no host-process fallback."""

    __slots__ = (
        "_host_executor_factory",
        "_locator",
        "_platform_name",
        "_runtime_certification_root",
        "_runtime_loader",
        "_runtime_review_sha256",
        "_runtime_reviewer",
    )

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        executable_locator: ExecutableLocator | None = None,
        runtime_loader: RuntimeCertificationLoader | None = None,
        host_executor_factory: HostExecutorFactory | None = None,
        certification_root: Path | None = None,
        trusted_reviewer: str | None = None,
        trusted_review_sha256: str | None = None,
    ) -> None:
        self._platform_name = os.name if platform_name is None else platform_name
        self._locator = (
            shutil.which if executable_locator is None else executable_locator
        )
        self._runtime_loader = runtime_loader
        self._host_executor_factory = host_executor_factory
        self._runtime_certification_root = certification_root
        self._runtime_reviewer = trusted_reviewer
        self._runtime_review_sha256 = trusted_review_sha256

    def probe(self) -> SandboxCapabilities:
        """Locate Windows Sandbox without launching it or creating files."""

        if self._platform_name != "nt":
            return SandboxCapabilities(
                backend=WINDOWS_SANDBOX_BACKEND,
                available=False,
                reason_code="unsupported_platform",
                summary="当前平台不是 Windows，Windows Sandbox 不可用。",
            )

        gui_executable = self._find_component(WINDOWS_SANDBOX_GUI_EXECUTABLE)
        cli_executable = self._find_component(WINDOWS_SANDBOX_CLI_EXECUTABLE)
        if gui_executable is None and cli_executable is None:
            return SandboxCapabilities(
                backend=WINDOWS_SANDBOX_BACKEND,
                available=False,
                reason_code="executable_not_found",
                summary="未找到 WindowsSandbox.exe 或 wsb.exe。",
            )
        if cli_executable is None:
            return SandboxCapabilities(
                backend=WINDOWS_SANDBOX_BACKEND,
                available=True,
                reason_code="cli_executable_required",
                summary=("已检测到 Windows Sandbox GUI，但缺少可认证的 wsb.exe CLI。"),
                workspace_modes=("none", "read-only-snapshot"),
                network_modes=("deny",),
            )
        try:
            material = self._load_runtime(cli_executable)
        except (SandboxError, ValueError) as error:
            reason_code = (
                "certification_required"
                if error.__class__.__name__ == "SandboxRuntimeCertificationUnavailable"
                else "certification_invalid"
            )
            return SandboxCapabilities(
                backend=WINDOWS_SANDBOX_BACKEND,
                available=True,
                reason_code=reason_code,
                summary=(
                    "已检测到 Windows Sandbox CLI，但没有可用于当前运行时的认证证据。"
                ),
                executable=cli_executable,
                workspace_modes=("none", "read-only-snapshot"),
                network_modes=("deny",),
            )
        return SandboxCapabilities(
            backend=WINDOWS_SANDBOX_BACKEND,
            available=True,
            reason_code="ready",
            summary="Windows Sandbox 认证、宿主绑定和全部强制门禁均已验证。",
            executable=cli_executable,
            certification=material.certification,
            workspace_modes=("read-only-snapshot",),
            network_modes=("deny",),
            supports_cancellation=True,
            supports_timeout=True,
            supports_output_limit=True,
            supports_memory_limit=True,
            supports_process_limit=True,
        )

    def build_config_xml(self, spec: RunSpec) -> str:
        """Build a restrictive in-memory ``.wsb`` document without launching."""

        snapshot = self._validated_snapshot(spec)
        if spec.limits.max_memory_bytes < WINDOWS_SANDBOX_MIN_MEMORY_BYTES:
            raise SandboxError("Windows Sandbox 无法可靠执行低于 2048 MiB 的内存上限。")
        configuration = ElementTree.Element("Configuration")
        _xml_setting(configuration, "VGpu", "Disable")
        _xml_setting(configuration, "Networking", "Disable")
        _xml_setting(configuration, "AudioInput", "Disable")
        _xml_setting(configuration, "VideoInput", "Disable")
        _xml_setting(configuration, "PrinterRedirection", "Disable")
        _xml_setting(configuration, "ClipboardRedirection", "Disable")
        _xml_setting(configuration, "ProtectedClient", "Enable")
        memory_mebibytes = math.ceil(spec.limits.max_memory_bytes / (1024 * 1024))
        _xml_setting(configuration, "MemoryInMB", str(memory_mebibytes))

        if snapshot is not None:
            mapped_folders = ElementTree.SubElement(
                configuration,
                "MappedFolders",
            )
            mapped_folder = ElementTree.SubElement(mapped_folders, "MappedFolder")
            _xml_setting(mapped_folder, "HostFolder", str(snapshot))
            _xml_setting(
                mapped_folder,
                "SandboxFolder",
                WINDOWS_SANDBOX_GUEST_WORKSPACE,
            )
            _xml_setting(mapped_folder, "ReadOnly", "true")

        # No LogonCommand is emitted: argv must never be interpolated into XML or
        # an implicit command shell before a trusted guest transport exists.
        return ElementTree.tostring(
            configuration,
            encoding="unicode",
            short_empty_elements=False,
        )

    def run(
        self,
        spec: RunSpec,
        *,
        cancel: CancellationSignal | None = None,
        approved_preview: str | None = None,
    ) -> SandboxResult:
        """Execute only after revalidating certification and every local binding."""

        cli_executable = self._require_certified_cli()
        material = self._load_runtime(cli_executable)
        manifest, snapshot, executable, binding = self._execution_binding(
            spec,
            material,
        )
        if (
            approved_preview is not None
            and binding.render_preview() != approved_preview
        ):
            raise SandboxError(
                "Windows Sandbox 认证或执行边界在批准后发生变化，命令已拒绝。"
            )

        from .sandbox_guest import (
            GUEST_BINARY_FILENAME,
            GUEST_REQUEST_FILENAME,
            MAX_ACTIVE_PROCESSES as GUEST_MAX_ACTIVE_PROCESSES,
            MAX_JOB_MEMORY_BYTES,
            MAX_OUTPUT_BYTES as GUEST_MAX_OUTPUT_BYTES,
            MAX_PROCESS_MEMORY_BYTES,
            MAX_TIMEOUT_MS,
            SandboxGuestRequest,
        )
        from .windows_sandbox import WsbExecutionPlan, WsbHostExecutor

        timeout_ms = min(round(spec.limits.timeout_seconds * 1_000), MAX_TIMEOUT_MS)
        if spec.limits.max_output_bytes > GUEST_MAX_OUTPUT_BYTES:
            raise SandboxError("Windows Sandbox 输出上限超过已认证 guest 协议边界。")
        if spec.limits.max_processes > GUEST_MAX_ACTIVE_PROCESSES:
            raise SandboxError("Windows Sandbox 进程上限超过已认证 guest 协议边界。")
        job_memory = min(spec.limits.max_memory_bytes, MAX_JOB_MEMORY_BYTES)
        process_memory = min(job_memory, MAX_PROCESS_MEMORY_BYTES)
        instance_id = uuid4()
        run_id = uuid4()
        request = SandboxGuestRequest.create(
            run_id=run_id.hex,
            instance_id=instance_id.hex,
            snapshot_manifest_sha256=manifest.digest,
            runner_source_sha256=material.certification.runner_source_sha256,
            approval_binding_sha256=binding.digest,
            executable=executable,
            argv=spec.argv,
            cwd=spec.working_directory,
            environment={},
            timeout_ms=timeout_ms,
            max_output_bytes=spec.limits.max_output_bytes,
            active_process_limit=spec.limits.max_processes,
            process_memory_bytes=process_memory,
            job_memory_bytes=job_memory,
        )
        try:
            with TemporaryDirectory(prefix="neil-agent-wsb-run-") as temporary:
                root = Path(temporary).resolve(strict=True)
                control = root / "control"
                transport = root / "transport"
                control.mkdir()
                transport.mkdir()
                shutil.copyfile(
                    material.runner_binary_path,
                    control / GUEST_BINARY_FILENAME,
                )
                with (control / GUEST_REQUEST_FILENAME).open("xb") as stream:
                    stream.write(request.canonical_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                plan = WsbExecutionPlan(
                    instance_id=instance_id,
                    run_id=run_id,
                    request_hash=request.request_hash,
                    snapshot_directory=snapshot,
                    control_directory=control,
                    temporary_root=transport,
                    snapshot_manifest_sha256=manifest.digest,
                    certification_sha256=(material.certification.certification_sha256),
                    runner_source_sha256=(material.certification.runner_source_sha256),
                    runner_sha256=material.certification.runner_binary_sha256,
                    approval_binding_version=request.approval_binding_version,
                    approval_binding_sha256=binding.digest,
                    timeout_seconds=max(60.0, spec.limits.timeout_seconds + 30.0),
                    export_paths=spec.export_paths,
                )
                factory = self._host_executor_factory
                executor = (
                    WsbHostExecutor(cli_executable)
                    if factory is None
                    else factory(cli_executable)
                )
                result = executor.execute(plan, cancel=cancel)
        except SandboxError:
            raise
        except (OSError, ValueError) as error:
            raise SandboxError(
                "Windows Sandbox 执行准备失败，未启动宿主回退进程。"
            ) from error
        return _map_wsb_result(result)

    def preview(self, spec: RunSpec) -> str:
        """Render the exact approval boundary for one prepared snapshot."""

        cli_executable = self._require_certified_cli()
        material = self._load_runtime(cli_executable)
        _, _, _, binding = self._execution_binding(spec, material)
        return binding.render_preview()

    def _load_runtime(self, cli_executable: Path) -> RuntimeCertificationMaterial:
        if self._runtime_loader is not None:
            return self._runtime_loader(cli_executable)
        from .sandbox_evidence import ReviewTrustPins
        from .sandbox_runtime import load_runtime_certification

        pins = None
        if (
            self._runtime_reviewer is not None
            or self._runtime_review_sha256 is not None
        ):
            pins = ReviewTrustPins(
                reviewer_ids=(
                    frozenset({self._runtime_reviewer})
                    if self._runtime_reviewer is not None
                    else frozenset()
                ),
                review_sha256s=(
                    frozenset({self._runtime_review_sha256})
                    if self._runtime_review_sha256 is not None
                    else frozenset()
                ),
            )
        return load_runtime_certification(
            cli_executable,
            certification_root=self._runtime_certification_root,
            trust_pins=pins,
        )

    def _require_certified_cli(self) -> Path:
        capabilities = self.probe()
        if not capabilities.ready or capabilities.executable is None:
            raise SandboxError(
                "Windows Sandbox 未通过当前运行时认证，命令已拒绝；不会退化为宿主进程。"
            )
        return capabilities.executable

    def _execution_binding(
        self,
        spec: RunSpec,
        material: RuntimeCertificationMaterial,
    ) -> tuple[SnapshotManifest, Path, str, RunCommandApprovalBinding]:
        from .sandbox_approval import RunCommandApprovalBinding
        from .sandbox_guest import (
            MAX_ACTIVE_PROCESSES as GUEST_MAX_ACTIVE_PROCESSES,
            MAX_JOB_MEMORY_BYTES,
            MAX_OUTPUT_BYTES as GUEST_MAX_OUTPUT_BYTES,
            MAX_PROCESS_MEMORY_BYTES,
            MAX_TIMEOUT_MS,
        )
        from .sandbox_snapshot import inspect_prepared_snapshot

        if (
            spec.policy.workspace_mode != "read-only-snapshot"
            or spec.policy.network != "deny"
            or spec.policy.environment
            or spec.workspace_snapshot is None
        ):
            raise SandboxError("认证执行只允许无环境变量的只读快照和禁网策略。")
        snapshot = self._validated_snapshot(spec)
        if snapshot is None:
            raise SandboxError("认证执行缺少只读工作区快照。")
        try:
            executable_path = spec.executable.resolve(strict=True)
            relative = executable_path.relative_to(snapshot)
        except (OSError, ValueError) as error:
            raise SandboxError("可执行文件必须位于已准备的只读快照内。") from error
        if not executable_path.is_file() or _is_reparse_point(executable_path):
            raise SandboxError("快照可执行文件不是安全普通文件。")
        executable = str(PurePosixPath(*relative.parts)).replace("/", "\\")
        manifest = inspect_prepared_snapshot(snapshot)
        timeout_ms = min(round(spec.limits.timeout_seconds * 1_000), MAX_TIMEOUT_MS)
        if (
            spec.limits.timeout_seconds * 1_000 > MAX_TIMEOUT_MS
            or spec.limits.max_output_bytes > GUEST_MAX_OUTPUT_BYTES
            or spec.limits.max_processes > GUEST_MAX_ACTIVE_PROCESSES
        ):
            raise SandboxError("命令限制超过已认证 guest 协议边界。")
        job_memory = min(spec.limits.max_memory_bytes, MAX_JOB_MEMORY_BYTES)
        binding = RunCommandApprovalBinding(
            executable=executable,
            argv=spec.argv,
            logical_cwd=spec.working_directory,
            snapshot_manifest_sha256=manifest.digest,
            certification_sha256=material.certification.certification_sha256,
            runner_source_sha256=material.certification.runner_source_sha256,
            runner_binary_sha256=material.certification.runner_binary_sha256,
            timeout_ms=timeout_ms,
            max_output_bytes=spec.limits.max_output_bytes,
            active_process_limit=spec.limits.max_processes,
            process_memory_bytes=min(job_memory, MAX_PROCESS_MEMORY_BYTES),
            job_memory_bytes=job_memory,
            export_paths=spec.export_paths,
        )
        return manifest, snapshot, executable, binding

    def _find_component(self, name: str) -> Path | None:
        located = self._locator(name)
        if not located:
            return None
        candidate = Path(located)
        if not candidate.is_absolute() or _path_has_reparse_component(candidate):
            return None
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        if (
            resolved.is_file()
            and not _is_reparse_point(resolved)
            and resolved.name.casefold() == name.casefold()
        ):
            return resolved
        return None

    @staticmethod
    def _validated_snapshot(spec: RunSpec) -> Path | None:
        snapshot = spec.workspace_snapshot
        if snapshot is None:
            return None
        if _path_has_reparse_component(snapshot):
            raise SandboxError("工作区快照必须是真实普通目录，不能是重解析点。")
        try:
            resolved = snapshot.resolve(strict=True)
        except OSError as error:
            raise SandboxError("工作区快照不存在或无法解析。") from error
        if not resolved.is_dir() or _is_reparse_point(resolved):
            raise SandboxError("工作区快照必须是真实普通目录，不能是重解析点。")
        if _is_filesystem_root(resolved):
            raise SandboxError("拒绝把卷根目录映射到 Windows Sandbox。")
        _validate_snapshot_contents(resolved)

        relative_working = PurePosixPath(spec.working_directory)
        working_directory = resolved.joinpath(*relative_working.parts)
        try:
            resolved_working = working_directory.resolve(strict=True)
            resolved_working.relative_to(resolved)
        except (OSError, ValueError) as error:
            raise SandboxError("沙箱工作目录不在只读快照内。") from error
        if not resolved_working.is_dir() or _is_reparse_point(resolved_working):
            raise SandboxError("沙箱工作目录必须是真实普通目录。")
        return resolved


def _map_wsb_result(result: WsbExecutionResult) -> SandboxResult:
    if result.job_terminated is not True:
        raise SandboxError("Windows Sandbox 未确认完整进程树终止，结果已拒绝。")
    if result.status == "exited":
        reason: TerminationReason = (
            "succeeded" if result.exit_code == 0 else "exit_nonzero"
        )
        exit_code = result.exit_code
    else:
        reason_by_status: dict[str, TerminationReason] = {
            "timeout": "timed_out",
            "cancelled": "cancelled",
            "output_limit": "output_limit",
            "resource_limit": "resource_limit",
            "runner_error": "backend_error",
        }
        reason = reason_by_status.get(result.status, "backend_error")
        exit_code = None
    return SandboxResult(
        backend=WINDOWS_SANDBOX_BACKEND,
        termination_reason=reason,
        exit_code=exit_code,
        stdout=result.stdout.decode("utf-8", errors="replace"),
        stderr=result.stderr.decode("utf-8", errors="replace"),
        elapsed_seconds=result.duration_ms / 1_000,
        run_id=result.run_id.hex,
        request_hash=result.request_hash,
        certification_sha256=result.certification_sha256,
        exported_files=result.exported_files,
    )


def _validate_bounded_integer(
    name: str,
    value: int,
    *,
    minimum: int,
    maximum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _contains_line_separator(value: str) -> bool:
    return any(character in value for character in ("\r", "\n", "\u2028", "\u2029"))


def _validate_environment_value(name: str, value: str) -> None:
    if name in {"LANG", "LC_ALL"} and not _LOCALE_VALUE.fullmatch(value):
        raise ValueError(f"sandbox {name} value is not a locale identifier")
    if name == "NO_COLOR" and value not in {"0", "1"}:
        raise ValueError("sandbox NO_COLOR must be 0 or 1")
    if name == "PYTHONUTF8" and value not in {"0", "1"}:
        raise ValueError("sandbox PYTHONUTF8 must be 0 or 1")
    if name == "TZ" and (
        not _TIME_ZONE_VALUE.fullmatch(value)
        or value.startswith(("/", "."))
        or ".." in value.split("/")
        or ":" in value
        or "\\" in value
    ):
        raise ValueError("sandbox TZ value is not a safe time-zone identifier")


def _normalize_relative_directory(value: str) -> str:
    if not isinstance(value, str) or not value or _contains_control_character(value):
        raise ValueError("sandbox working directory is invalid")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or ":" in normalized
        or any(part == ".." for part in path.parts)
    ):
        raise ValueError("sandbox working directory must stay inside the snapshot")
    compact = path.as_posix()
    return "." if compact in {"", "."} else compact


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def _path_has_reparse_component(path: Path) -> bool:
    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if _is_reparse_point(current):
            return True
    return False


def _is_filesystem_root(path: Path) -> bool:
    return path.parent == path


def _validate_snapshot_contents(root: Path) -> None:
    pending = [root]
    seen_entries = 0
    total_bytes = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen_entries += 1
                    if seen_entries > MAX_SNAPSHOT_ENTRIES:
                        raise SandboxError("工作区快照条目过多，无法安全验证。")
                    if is_sensitive_entry_name(entry.name):
                        raise SandboxError("工作区快照包含受保护目录或敏感文件。")
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        raise SandboxError("无法验证工作区快照条目。") from error
                    attributes = int(getattr(metadata, "st_file_attributes", 0))
                    if entry.is_symlink() or attributes & _REPARSE_POINT_ATTRIBUTE:
                        raise SandboxError("工作区快照不能包含重解析点。")
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(Path(entry.path))
                    elif not stat.S_ISREG(metadata.st_mode):
                        raise SandboxError("工作区快照只能包含普通文件和目录。")
                    elif _file_link_count(Path(entry.path), metadata) > 1:
                        raise SandboxError("工作区快照不能包含硬链接文件。")
                    elif metadata.st_size > MAX_SNAPSHOT_FILE_BYTES:
                        raise SandboxError("工作区快照包含超过单文件上限的文件。")
                    else:
                        total_bytes += metadata.st_size
                        if total_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
                            raise SandboxError("工作区快照超过累计字节上限。")
        except SandboxError:
            raise
        except OSError as error:
            raise SandboxError("无法扫描工作区快照。") from error


def _file_link_count(path: Path, metadata: os.stat_result) -> int:
    if os.name != "nt":
        return int(metadata.st_nlink)
    return _windows_file_link_count(path)


def _windows_file_link_count(
    path: Path,
    *,
    api: _WindowsFileApi | None = None,
) -> int:
    try:
        windows_api = _CtypesWindowsFileApi() if api is None else api
        handle = windows_api.open_metadata(path)
    except OSError as error:
        raise SandboxError("无法打开工作区快照文件进行硬链接验证。") from error

    information: _WindowsFileInformation | None = None
    query_error: OSError | None = None
    try:
        information = windows_api.query(handle)
    except OSError as error:
        query_error = error

    try:
        windows_api.close(handle)
    except OSError as error:
        raise SandboxError("无法可靠关闭工作区快照文件句柄。") from error

    if query_error is not None:
        raise SandboxError("无法查询工作区快照文件的硬链接信息。") from query_error
    if information is None:
        raise SandboxError("工作区快照文件的硬链接信息缺失。")
    if information.file_attributes & _REPARSE_POINT_ATTRIBUTE:
        raise SandboxError("工作区快照不能包含重解析点。")
    if information.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY:
        raise SandboxError("工作区快照文件在验证期间发生类型变化。")
    if information.link_count < 1:
        raise SandboxError("工作区快照文件的硬链接计数无效。")
    return information.link_count


def _xml_setting(parent: ElementTree.Element, name: str, value: str) -> None:
    ElementTree.SubElement(parent, name).text = value
