"""One-shot, read-only agent execution with stable output protocols."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, TextIO
from unicodedata import category

from .agent import Agent, ChatModel
from .config import Settings
from .errors import NeilAgentError
from .hooks import LifecycleHooks
from .instructions import ProjectInstructionManager
from .llm import LLMClient
from .schemas import ActivityEvent
from .session import SessionStore
from .tools import FileSystemTools, ShellTools, ToolRegistry

OutputFormat = Literal["text", "json", "stream-json"]
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
        }
        if self.output_format == "json":
            payload["activities"] = self._activities
        self._write_json(payload)

    def error(self, *, message: str, exit_code: int) -> None:
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
        writer.error(message="prompt 不能为空。", exit_code=2)
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
            hooks=hooks,
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
            session_store.save(session, agent.messages, (), None, create_only=True)
            saved = True
        writer.success(session_id=session.session_id, result=result, saved=saved)
        return 0
    except KeyboardInterrupt:
        writer.error(message="用户中断。", exit_code=130)
        return 130
    except ValueError as error:
        writer.error(message=str(error), exit_code=2)
        return 2
    except NeilAgentError as error:
        writer.error(message=str(error), exit_code=1)
        return 1
    except Exception:  # noqa: BLE001 - protocol boundary hides internals.
        writer.error(message="内部错误，请查看本地测试或日志。", exit_code=1)
        return 1


def write_startup_error(
    output_format: OutputFormat,
    stdout: TextIO,
    stderr: TextIO,
    message: str,
) -> None:
    """Emit a configuration error before a session can be constructed."""

    ProtocolWriter(output_format, stdout, stderr).error(message=message, exit_code=2)


def _instruction_target(workspace_root: Path) -> Path:
    try:
        current = Path.cwd().resolve(strict=True)
        current.relative_to(workspace_root)
    except (OSError, ValueError):
        return workspace_root
    return current if current.is_dir() else workspace_root


def _safe_protocol_text(value: str) -> str:
    normalized = " ".join(value.split())[:MAX_PROTOCOL_ERROR_CHARS]
    return "".join(
        character
        for character in normalized
        if not category(character).startswith("C")
    ) or "未知错误"
