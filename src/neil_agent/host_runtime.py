"""Shared Agent host runtime assembly for CLI, non-interactive, and Web entry points."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from .audit import JsonlAuditSink
from .config import Settings
from .hooks import LifecycleHooks
from .instructions import ProjectInstructionManager
from .task import PlanChangeHandler, TaskTracker
from .tools.filesystem import FileSystemTools
from .tools.registry import ToolRegistry
from .tools.sandbox import SandboxCommandTools
from .tools.shell import ShellTools
from .sandbox import WindowsSandboxBackend


class HostMode(str, Enum):
    """Which Neil Agent entry point is constructing a runtime."""

    CLI = "cli"
    NONINTERACTIVE_READONLY = "noninteractive-read-only"
    NONINTERACTIVE_WRITE = "noninteractive-write"
    WEB = "web"


InstructionScope = Literal["cwd", "workspace_root"]


@dataclass(frozen=True, slots=True)
class HostProfile:
    """Documented capabilities for one assembled host runtime."""

    mode: HostMode
    tool_names: tuple[str, ...]
    sandbox_tools_enabled: bool
    instruction_scope: InstructionScope
    task_tools_enabled: bool
    audit_enabled: bool


@dataclass(frozen=True, slots=True)
class HostRuntime:
    """Shared filesystem, registry, and instruction state for one host session."""

    filesystem: FileSystemTools
    shell: ShellTools
    registry: ToolRegistry
    instruction_manager: ProjectInstructionManager
    hooks: LifecycleHooks
    audit_sink: JsonlAuditSink | None
    task_tracker: TaskTracker | None
    profile: HostProfile


def instruction_target(workspace_root: Path) -> Path:
    """Use the launch directory when it is safely inside the configured workspace."""

    try:
        current = Path.cwd().resolve(strict=True)
        current.relative_to(workspace_root)
    except (OSError, ValueError):
        return workspace_root
    return current if current.is_dir() else workspace_root


def windows_sandbox_backend(settings: Settings) -> WindowsSandboxBackend:
    """Construct the configured Windows Sandbox backend for tool registration."""

    return WindowsSandboxBackend(
        certification_root=settings.sandbox_certification_root,
        trusted_reviewer=settings.sandbox_trusted_reviewer,
        trusted_review_sha256=settings.sandbox_trusted_review_sha256,
    )


def _register_sandbox_tools(
    settings: Settings,
    filesystem: FileSystemTools,
    registry: ToolRegistry,
) -> bool:
    if settings.sandbox_backend != "windows-sandbox":
        return False
    SandboxCommandTools(
        filesystem.root,
        windows_sandbox_backend(settings),
        timeout_seconds=settings.command_timeout,
        max_output_bytes=settings.max_command_output_chars,
    ).register_if_ready(registry)
    return any(definition.name == "run_command" for definition in registry.definitions)


def _instruction_scope_for_mode(
    mode: HostMode,
    workspace_root: Path,
) -> tuple[ProjectInstructionManager, InstructionScope]:
    if mode is HostMode.WEB:
        return ProjectInstructionManager(workspace_root), "workspace_root"
    target = instruction_target(workspace_root)
    return ProjectInstructionManager(workspace_root, target), "cwd"


def build_host_runtime(
    settings: Settings,
    *,
    mode: HostMode,
    task_change_handler: PlanChangeHandler | None = None,
    base_hooks: LifecycleHooks | None = None,
) -> HostRuntime:
    """Assemble the shared tool registry and instruction context for one host."""

    filesystem = FileSystemTools(settings.workspace_root)
    registry = ToolRegistry()
    shell = ShellTools(
        filesystem.root,
        timeout=settings.command_timeout,
        max_output_chars=settings.max_command_output_chars,
    )
    if mode is HostMode.NONINTERACTIVE_READONLY:
        filesystem.register_read_only(registry)
        shell.register_read_only(registry)
    else:
        filesystem.register(registry)
        shell.register(registry)

    sandbox_tools_enabled = False
    if mode in {HostMode.CLI, HostMode.NONINTERACTIVE_WRITE}:
        sandbox_tools_enabled = _register_sandbox_tools(settings, filesystem, registry)

    instruction_manager, instruction_scope = _instruction_scope_for_mode(
        mode,
        filesystem.root,
    )

    hooks = base_hooks.copy() if base_hooks is not None else LifecycleHooks()
    audit_sink: JsonlAuditSink | None = None
    if settings.audit_log_enabled:
        audit_sink = JsonlAuditSink(
            filesystem.root,
            max_bytes=settings.audit_log_max_bytes,
        )
        audit_sink.register(hooks)

    task_tracker: TaskTracker | None = None
    if mode in {HostMode.CLI, HostMode.WEB}:
        task_tracker = TaskTracker(change_handler=task_change_handler)
        task_tracker.register(registry)

    profile = HostProfile(
        mode=mode,
        tool_names=tuple(definition.name for definition in registry.definitions),
        sandbox_tools_enabled=sandbox_tools_enabled,
        instruction_scope=instruction_scope,
        task_tools_enabled=task_tracker is not None,
        audit_enabled=settings.audit_log_enabled,
    )
    return HostRuntime(
        filesystem=filesystem,
        shell=shell,
        registry=registry,
        instruction_manager=instruction_manager,
        hooks=hooks,
        audit_sink=audit_sink,
        task_tracker=task_tracker,
        profile=profile,
    )
