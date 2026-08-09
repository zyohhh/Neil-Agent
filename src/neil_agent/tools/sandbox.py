"""Conditionally exposed, shell-free Windows Sandbox command tool."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory

from ..errors import SandboxError, ToolError
from ..sandbox import (
    MAX_ARGUMENTS,
    MIN_OUTPUT_BYTES,
    SandboxLimits,
    SandboxPolicy,
    RunSpec,
    WindowsSandboxBackend,
)
from ..sandbox_guest import MAX_OUTPUT_BYTES, MAX_TIMEOUT_MS
from ..sandbox_snapshot import prepare_snapshot
from ..schemas import ToolDefinition
from .registry import ToolRegistry

RUN_COMMAND = ToolDefinition(
    name="run_command",
    description=(
        "Run one explicit .exe plus argv in a certified, network-disabled "
        "Windows Sandbox over a read-only workspace snapshot. Guest changes "
        "are always discarded."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "executable": {
                "type": "string",
                "description": "Workspace-relative path to one explicit .exe.",
            },
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_ARGUMENTS,
                "description": "Argument vector; no shell command string.",
            },
        },
        "required": ["executable", "argv"],
        "additionalProperties": False,
    },
)


class SandboxCommandTools:
    """Register only when the backend has a verified runtime capability."""

    def __init__(
        self,
        workspace_root: Path,
        backend: WindowsSandboxBackend,
        *,
        timeout_seconds: float = 120.0,
        max_output_bytes: int = 20_000,
    ) -> None:
        try:
            self._workspace = workspace_root.resolve(strict=True)
        except OSError as error:
            raise ValueError("sandbox command workspace is unavailable") from error
        if not self._workspace.is_dir():
            raise ValueError("sandbox command workspace must be a directory")
        self._backend = backend
        self._limits = SandboxLimits(
            timeout_seconds=min(timeout_seconds, MAX_TIMEOUT_MS / 1_000),
            max_output_bytes=max(
                MIN_OUTPUT_BYTES,
                min(max_output_bytes, MAX_OUTPUT_BYTES),
            ),
        )
        self._latest_preview: str | None = None

    def register_if_ready(self, registry: ToolRegistry) -> bool:
        capabilities = self._backend.probe()
        if not capabilities.ready:
            return False
        registry.register(
            RUN_COMMAND,
            self.run_command,
            requires_approval=True,
            preview_handler=self.preview_run_command,
        )
        return True

    def preview_run_command(self, executable: str, argv: list[str]) -> str:
        with self._prepared_spec(executable, argv) as spec:
            try:
                preview = self._backend.preview(spec)
            except (SandboxError, ValueError) as error:
                raise ToolError("Windows Sandbox 命令预览未通过安全门禁。") from error
        self._latest_preview = preview
        return preview

    def run_command(self, executable: str, argv: list[str]) -> str:
        with self._prepared_spec(executable, argv) as spec:
            try:
                current_preview = self._backend.preview(spec)
                if self._latest_preview != current_preview:
                    raise ToolError("工作区或认证边界已变化，请重新预览并批准。")
                result = self._backend.run(
                    spec,
                    approved_preview=current_preview,
                )
            except ToolError:
                raise
            except (SandboxError, ValueError) as error:
                raise ToolError(
                    "Windows Sandbox 命令被安全门禁拒绝；未执行宿主回退。"
                ) from error
            finally:
                self._latest_preview = None
        return json.dumps(
            {
                "backend": result.backend,
                "termination_reason": result.termination_reason,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "elapsed_seconds": result.elapsed_seconds,
                "guest_modifications": "discarded",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @contextmanager
    def _prepared_spec(
        self,
        executable: str,
        argv: list[str],
    ) -> Iterator[RunSpec]:
        relative = _validate_executable(executable)
        arguments = _validate_argv(argv)
        try:
            with TemporaryDirectory(prefix="neil-agent-command-snapshot-") as temporary:
                destination = Path(temporary).resolve(strict=True) / "snapshot"
                with prepare_snapshot(self._workspace, destination) as snapshot:
                    snapshot_executable = snapshot.root.joinpath(*relative.parts)
                    if not snapshot_executable.is_file():
                        raise ToolError(
                            "可执行文件不在安全快照中，或被敏感路径过滤器拒绝。"
                        )
                    yield RunSpec(
                        executable=snapshot_executable,
                        argv=arguments,
                        policy=SandboxPolicy(
                            workspace_mode="read-only-snapshot",
                            network="deny",
                            environment=(),
                        ),
                        limits=self._limits,
                        workspace_snapshot=snapshot.root,
                    )
        except ToolError:
            raise
        except (OSError, SandboxError, ValueError) as error:
            raise ToolError("无法建立安全、只读的命令工作区快照。") from error


def _validate_executable(value: str) -> PureWindowsPath:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 240
        or "\0" in value
        or "\n" in value
        or "\r" in value
    ):
        raise ToolError("executable 必须是有界的工作区相对 .exe 路径。")
    path = PureWindowsPath(value)
    if (
        path.is_absolute()
        or path.drive
        or path.root
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix.lower() != ".exe"
    ):
        raise ToolError("executable 必须是工作区内显式、相对的 .exe 路径。")
    return path


def _validate_argv(value: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_ARGUMENTS:
        raise ToolError("argv 必须是有界字符串数组。")
    if any(not isinstance(item, str) for item in value):
        raise ToolError("argv 只能包含字符串。")
    return tuple(value)
