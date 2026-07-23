"""One-shot agent execution with stable, explicitly versioned protocols."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, TextIO
from unicodedata import category

from .agent import Agent, ChatModel
from .approval import (
    ApprovalRequest,
    ApprovalStore,
    NoninteractiveApprovalBroker,
)
from .audit import JsonlAuditSink
from .config import Settings
from .errors import (
    AgentError,
    ApprovalError,
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
ProtocolVersion = Literal[1, 2]
PermissionMode = Literal["read-only", "request", "approve"]
ErrorCode = Literal[
    "agent_error",
    "approval_error",
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
SUPPORTED_ERROR_CODES_V2: tuple[ErrorCode, ...] = (
    "agent_error",
    "approval_error",
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
PROTOCOL_VERSION: Literal[1] = 1
LATEST_PROTOCOL_VERSION: Literal[2] = 2
APPROVAL_REQUIRED_EXIT_CODE = 3
MAX_PROTOCOL_ERROR_CHARS = 1_000


class ProtocolWriter:
    """Render one run without mixing human decoration into structured output."""

    def __init__(
        self,
        output_format: OutputFormat,
        stdout: TextIO,
        stderr: TextIO,
        *,
        protocol_version: ProtocolVersion = PROTOCOL_VERSION,
        permission_mode: PermissionMode = "read-only",
    ) -> None:
        self.output_format = output_format
        self.stdout = stdout
        self.stderr = stderr
        self.protocol_version = protocol_version
        self.permission_mode = permission_mode
        self._activities: list[dict[str, object]] = []
        self._approval_requests: list[dict[str, object]] = []
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
            payload: dict[str, object] = {
                "type": "session_start",
                "protocol_version": self.protocol_version,
                "session_id": session_id,
                "model": model,
                "workspace": str(workspace),
                "tools": list(tools),
                "read_only": self.permission_mode == "read-only",
            }
            if self.protocol_version >= 2:
                payload["permission_mode"] = self.permission_mode
            self._write_json(payload)

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

    def approval_request(self, request: ApprovalRequest) -> None:
        """Record one bounded preview and stream it in protocol v2."""

        if self.protocol_version < 2:
            raise ApprovalError("审批请求只能使用非交互协议 v2。")
        payload: dict[str, object] = {
            "approval_id": request.approval_id,
            "tool_name": request.tool_name,
            "preview": request.preview,
            "expires_at": request.expires_at.isoformat(),
        }
        self._approval_requests.append(payload)
        if self.output_format == "stream-json":
            self._write_json(
                {
                    "type": "approval_request",
                    "protocol_version": self.protocol_version,
                    **payload,
                }
            )

    def success(
        self,
        *,
        session_id: str,
        result: str,
        saved: bool,
        usage: TokenUsage | None,
        approved_request_id: str | None = None,
    ) -> None:
        if self.output_format == "text":
            if not self._wrote_delta:
                self.stdout.write(result)
                self._delta_ends_with_newline = result.endswith(("\n", "\r"))
            if not self._delta_ends_with_newline:
                self.stdout.write("\n")
            self.stdout.flush()
            return
        payload: dict[str, object] = {
            "type": "result",
            "protocol_version": self.protocol_version,
            "success": True,
            "session_id": session_id,
            "result": result,
            "saved": saved,
            "usage": _usage_payload(usage),
        }
        if self.protocol_version >= 2:
            payload["permission_mode"] = self.permission_mode
            payload["approved_request_id"] = approved_request_id
        if self.output_format == "json":
            payload["activities"] = self._activities
        self._write_json(payload)

    def approval_required(
        self,
        *,
        session_id: str,
        result: str,
        usage: TokenUsage | None,
        approved_request_id: str | None,
    ) -> None:
        """Terminate protocol v2 with exact previews awaiting approval."""

        if self.protocol_version < 2:
            raise ApprovalError("审批请求只能使用非交互协议 v2。")
        payload: dict[str, object] = {
            "type": "approval_required",
            "protocol_version": self.protocol_version,
            "success": False,
            "session_id": session_id,
            "result": result,
            "saved": False,
            "usage": _usage_payload(usage),
            "permission_mode": self.permission_mode,
            "approved_request_id": approved_request_id,
            "approval_requests": self._approval_requests,
            "exit_code": APPROVAL_REQUIRED_EXIT_CODE,
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
        payload: dict[str, object] = {
            "type": "error",
            "protocol_version": self.protocol_version,
            "success": False,
            "error": safe_message,
            "error_code": error_code,
            "exit_code": exit_code,
        }
        if self.protocol_version >= 2:
            payload["permission_mode"] = self.permission_mode
        self._write_json(payload)

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
    protocol_version: ProtocolVersion = PROTOCOL_VERSION,
    permission_mode: PermissionMode = "read-only",
    approval_id: str | None = None,
    llm: ChatModel | None = None,
    hooks: LifecycleHooks | None = None,
) -> int:
    """Run one prompt and return an explicit, protocol-safe exit code."""

    writer = ProtocolWriter(
        output_format,
        stdout,
        stderr,
        protocol_version=protocol_version,
        permission_mode=permission_mode,
    )
    if not prompt.strip():
        writer.error(
            message="prompt 不能为空。",
            exit_code=2,
            error_code="invalid_input",
        )
        return 2
    validation_error = validate_noninteractive_options(
        output_format=output_format,
        protocol_version=protocol_version,
        permission_mode=permission_mode,
        approval_id=approval_id,
    )
    if validation_error is not None:
        writer.error(
            message=validation_error,
            exit_code=2,
            error_code="invalid_input",
        )
        return 2
    try:
        filesystem = FileSystemTools(settings.workspace_root)
        registry = ToolRegistry()
        shell = ShellTools(
            filesystem.root,
            timeout=settings.command_timeout,
            max_output_chars=settings.max_command_output_chars,
        )
        if permission_mode == "read-only":
            filesystem.register_read_only(registry)
            shell.register_read_only(registry)
        else:
            filesystem.register(registry)
            shell.register(registry)
        instruction_manager = ProjectInstructionManager(
            filesystem.root,
            _instruction_target(filesystem.root),
        )
        approval_broker: NoninteractiveApprovalBroker | None = None
        if permission_mode != "read-only":
            approval_broker = NoninteractiveApprovalBroker(
                ApprovalStore(filesystem.root),
                mode=permission_mode,
                prompt=prompt,
                instructions=lambda: instruction_manager.current.prompt_section(),
                request_handler=writer.approval_request,
                approval_id=approval_id,
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
            approval_handler=approval_broker,
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
        if approval_broker is not None and approval_broker.requests:
            writer.approval_required(
                session_id=session.session_id,
                result=result,
                usage=agent.last_usage,
                approved_request_id=approval_broker.consumed_request_id,
            )
            return APPROVAL_REQUIRED_EXIT_CODE
        if (
            permission_mode == "approve"
            and approval_broker is not None
            and approval_broker.consumed_request_id is None
        ):
            raise ApprovalError("模型未请求与 approval ID 匹配的操作，未执行写入。")
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
            approved_request_id=(
                approval_broker.consumed_request_id
                if approval_broker is not None
                else None
            ),
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
    *,
    protocol_version: ProtocolVersion = PROTOCOL_VERSION,
    permission_mode: PermissionMode = "read-only",
) -> None:
    """Emit a configuration error before a session can be constructed."""

    ProtocolWriter(
        output_format,
        stdout,
        stderr,
        protocol_version=protocol_version,
        permission_mode=permission_mode,
    ).error(
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
    if isinstance(error, ApprovalError):
        return "approval_error"
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


def validate_noninteractive_options(
    *,
    output_format: OutputFormat,
    protocol_version: ProtocolVersion,
    permission_mode: PermissionMode,
    approval_id: str | None,
) -> str | None:
    if protocol_version not in {PROTOCOL_VERSION, LATEST_PROTOCOL_VERSION}:
        return "仅支持非交互协议版本 1 或 2。"
    if permission_mode == "read-only":
        if approval_id is not None:
            return "只读模式不能提供 approval ID。"
        return None
    if protocol_version < 2:
        return "非交互写入审批必须显式使用协议版本 2。"
    if output_format == "text":
        return "非交互写入审批只支持 json 或 stream-json 输出。"
    if permission_mode == "request" and approval_id is not None:
        return "request 模式不能提供 approval ID。"
    if permission_mode == "approve" and approval_id is None:
        return "approve 模式必须提供 approval ID。"
    return None


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
