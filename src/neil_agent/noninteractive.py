"""One-shot, read-only agent execution with stable output protocols."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, TextIO
from unicodedata import category

from .agent import Agent, ChatModel
from .audit import JsonlAuditSink
from .config import Settings
from .errors import (
    AgentError,
    AuditError,
    HookError,
    InstructionError,
    LLMError,
    NeilAgentError,
    SessionError,
    ToolError,
)
from .hooks import LifecycleHooks
from .instructions import ProjectInstructionManager
from .llm import LLMClient
from .schemas import ActivityEvent, TokenUsage
from .session import SessionStore
from .tools import FileSystemTools, ShellTools, ToolRegistry

OutputFormat = Literal["text", "json", "stream-json"]
ErrorCode = Literal[
    "agent_error",
    "audit_error",
    "configuration_error",
    "hook_error",
    "invalid_input",
    "instruction_error",
    "internal_error",
    "interrupted",
    "model_error",
    "session_error",
    "tool_error",
]
SUPPORTED_ERROR_CODES: tuple[ErrorCode, ...] = (
    "agent_error",
    "audit_error",
    "configuration_error",
    "hook_error",
    "invalid_input",
    "instruction_error",
    "internal_error",
    "interrupted",
    "model_error",
    "session_error",
    "tool_error",
)
PROTOCOL_VERSION = 1
MAX_PROTOCOL_ERROR_CHARS = 1_000


class ProtocolWriter:
    """Render one run without mixing human decoration into structured output."""

    def __init__(
        self,
        output_format: OutputFormat,
        stdout: TextIO,
        stderr: TextIO,
    ) -> None:
        self.output_format = output_format
        self.stdout = stdout
        self.stderr = stderr
        self._activities: list[dict[str, object]] = []
        self._wrote_delta = False
        self._delta_ends_with_newline = False

    def start(
        self,
        *,
        session_id: str,
        model: str,
        workspace: Path,
        tools: tuple[str, ...],
    ) -> None:
        if self.output_format == "stream-json":
            self._write_json(
                {
                    "type": "session_start",
                    "protocol_version": PROTOCOL_VERSION,
                    "session_id": session_id,
                    "model": model,
                    "workspace": str(workspace),
                    "tools": list(tools),
                    "read_only": True,
                }
            )

    def activity(self, event: ActivityEvent) -> None:
        payload: dict[str, object] = {
            "status": event.status,
            "message": event.message,
            "details": list(event.details),
        }
        self._activities.append(payload)
        if self.output_format == "stream-json":
            self._write_json({"type": "activity", **payload})

    def text_delta(self, text: str) -> None:
        self._wrote_delta = True
        self._delta_ends_with_newline = text.endswith(("\n", "\r"))
        if self.output_format == "text":
            self.stdout.write(text)
            self.stdout.flush()
        elif self.output_format == "stream-json":
            self._write_json({"type": "text_delta", "text": text})

    def success(
        self,
        *,
        session_id: str,
        result: str,
        saved: bool,
        usage: TokenUsage | None,
    ) -> None:
        if self.output_format == "text":
            if not self._wrote_delta:
                self.stdout.write(result)
                self._delta_ends_with_newline = result.endswith(("\n", "\r"))
            if not self._delta_ends_with_newline:
                self.stdout.write("\n")
            self.stdout.flush()
            return
        payload = {
            "type": "result",
            "protocol_version": PROTOCOL_VERSION,
            "success": True,
            "session_id": session_id,
            "result": result,
            "saved": saved,
            "usage": _usage_payload(usage),
        }
        if self.output_format == "json":
            payload["activities"] = self._activities
        self._write_json(payload)

    def error(
        self,
        *,
        message: str,
        exit_code: int,
        error_code: ErrorCode,
    ) -> None:
        safe_message = _safe_protocol_text(message)
        if self.output_format == "text":
            self.stderr.write(f"Neil Agent 运行失败：{safe_message}\n")
            self.stderr.flush()
            return
        self._write_json(
            {
                "type": "error",
                "protocol_version": PROTOCOL_VERSION,
                "success": False,
                "error": safe_message,
                "error_code": error_code,
                "exit_code": exit_code,
            }
        )

    def _write_json(self, payload: dict[str, object]) -> None:
        self.stdout.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        self.stdout.flush()


def run_noninteractive(
    settings: Settings,
    prompt: str,
    *,
    output_format: OutputFormat,
    stdout: TextIO,
    stderr: TextIO,
    save_session: bool = False,
    llm: ChatModel | None = None,
    hooks: LifecycleHooks | None = None,
) -> int:
    """Run one prompt with read-only tools and return an explicit exit code."""

    writer = ProtocolWriter(output_format, stdout, stderr)
    if not prompt.strip():
        writer.error(
            message="prompt 不能为空。",
            exit_code=2,
            error_code="invalid_input",
        )
        return 2
    try:
        filesystem = FileSystemTools(settings.workspace_root)
        registry = ToolRegistry()
        filesystem.register_read_only(registry)
        shell = ShellTools(
            filesystem.root,
            timeout=settings.command_timeout,
            max_output_chars=settings.max_command_output_chars,
        )
        shell.register_read_only(registry)
        instruction_manager = ProjectInstructionManager(
            filesystem.root,
            _instruction_target(filesystem.root),
        )
        session_store = SessionStore(filesystem.root)
        session = session_store.new_session()
        model = llm or LLMClient(settings, retry_handler=writer.activity)
        active_hooks = hooks.copy() if hooks is not None else LifecycleHooks()
        if settings.audit_log_enabled:
            JsonlAuditSink(
                filesystem.root,
                max_bytes=settings.audit_log_max_bytes,
            ).register(active_hooks)
        agent = Agent(
            model,
            system_prompt=settings.system_prompt,
            project_instructions=instruction_manager.current.prompt_section(),
            max_rounds=settings.max_rounds,
            max_context_chars=settings.max_context_chars,
            max_context_tokens=settings.max_context_tokens,
            registry=registry,
            max_tool_rounds=settings.max_tool_rounds,
            activity_handler=writer.activity,
            instruction_scope_handler=instruction_manager.resolve_tool_call,
            hooks=active_hooks,
        )
        writer.start(
            session_id=session.session_id,
            model=settings.deepseek_model,
            workspace=filesystem.root,
            tools=tuple(definition.name for definition in registry.definitions),
        )
        for chunk in agent.stream_chat(prompt):
            writer.text_delta(chunk)
        result = agent.messages[-1].content
        saved = False
        if save_session:
            session_store.save(
                session,
                agent.messages,
                (),
                None,
                last_usage=agent.last_usage,
                create_only=True,
            )
            saved = True
        writer.success(
            session_id=session.session_id,
            result=result,
            saved=saved,
            usage=agent.last_usage,
        )
        return 0
    except KeyboardInterrupt:
        writer.error(
            message="用户中断。",
            exit_code=130,
            error_code="interrupted",
        )
        return 130
    except ValueError as error:
        writer.error(
            message=str(error),
            exit_code=2,
            error_code="configuration_error",
        )
        return 2
    except NeilAgentError as error:
        writer.error(
            message=str(error),
            exit_code=1,
            error_code=_error_code(error),
        )
        return 1
    except Exception:  # noqa: BLE001 - protocol boundary hides internals.
        writer.error(
            message="内部错误，请查看本地测试或日志。",
            exit_code=1,
            error_code="internal_error",
        )
        return 1


def write_startup_error(
    output_format: OutputFormat,
    stdout: TextIO,
    stderr: TextIO,
    message: str,
) -> None:
    """Emit a configuration error before a session can be constructed."""

    ProtocolWriter(output_format, stdout, stderr).error(
        message=message,
        exit_code=2,
        error_code="configuration_error",
    )


def _instruction_target(workspace_root: Path) -> Path:
    try:
        current = Path.cwd().resolve(strict=True)
        current.relative_to(workspace_root)
    except (OSError, ValueError):
        return workspace_root
    return current if current.is_dir() else workspace_root


def _safe_protocol_text(value: str) -> str:
    normalized = " ".join(value.split())[:MAX_PROTOCOL_ERROR_CHARS]
    return (
        "".join(
            character
            for character in normalized
            if not category(character).startswith("C")
        )
        or "未知错误"
    )


def _usage_payload(usage: TokenUsage | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        **usage.model_dump(),
        "total_tokens": usage.total_tokens,
    }


def _error_code(error: NeilAgentError) -> ErrorCode:
    if _has_cause(error, AuditError):
        return "audit_error"
    if isinstance(error, LLMError):
        return "model_error"
    if isinstance(error, AgentError):
        return "agent_error"
    if isinstance(error, ToolError):
        return "tool_error"
    if isinstance(error, SessionError):
        return "session_error"
    if isinstance(error, InstructionError):
        return "instruction_error"
    if isinstance(error, HookError):
        return "hook_error"
    return "internal_error"


def _has_cause(error: BaseException, error_type: type[BaseException]) -> bool:
    """Return whether an exception or its explicit cause has the given type."""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, error_type):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False
