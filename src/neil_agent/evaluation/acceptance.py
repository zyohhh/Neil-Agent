"""Explicit real DeepSeek protocol acceptance, separate from coding scores."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory


from ..agent import (
    Agent,
    COMPACTION_CHECKPOINT_USER,
    ChatModel,
    UsageReportingChatModel,
)
from ..config import Settings
from ..instructions import (
    load_project_instructions,
)
from ..providers.factory import create_provider
from ..noninteractive import run_noninteractive
from ..schemas import (
    ActivityEvent,
    Message,
    ModelResponse,
    ToolDefinition,
)
from ..session import SessionStore
from ..host_runtime import (
    BENCHMARK_MINIMAL_READONLY_TOOLS,
    RuntimeProfile,
)
from ..tools import FileSystemTools, ToolRegistry
from ..tools.filesystem import READ_FILE


from .common import EvalResult, _parse_json_lines, _parse_json_object, _parse_usage
from .fixtures import OfflineModel

REAL_V1_PROMPT = (
    'Call read_file exactly once with path "evidence.txt". Then return the file '
    "contents and the verification token required by the project instructions, "
    "with no explanation."
)
REAL_V2_PROMPT = (
    'You must call write_file exactly once with path "v2-approved.txt" and content '
    '"NEIL_V2_APPROVED". Do not call any other tool. After receiving any tool result, '
    "do not call a tool again; return a short completion message."
)
REAL_SYSTEM_PROMPT = (
    "You are running a bounded acceptance check. Follow the exact user request, "
    "use only the requested tool, and do not perform unrelated work."
)
REAL_V1_TOOLS = BENCHMARK_MINIMAL_READONLY_TOOLS
RealModelFactory = Callable[
    [str, Settings, Callable[[ActivityEvent], None]],
    ChatModel,
]


@dataclass(frozen=True, slots=True)
class _RealV1Outcome:
    instruction_result: EvalResult
    protocol_result: EvalResult
    messages: tuple[Message, ...] | None


def run_real_deepseek_acceptance(
    settings: Settings,
    *,
    runtime_profile: RuntimeProfile = RuntimeProfile.BENCHMARK_MINIMAL,
    model_factory: RealModelFactory | None = None,
) -> tuple[EvalResult, ...]:
    """Run bounded real-model checks after the CLI's explicit cost opt-in."""

    retry_events: list[ActivityEvent] = []
    factory = model_factory or _default_real_model_factory
    try:
        with TemporaryDirectory(prefix="neil-agent-real-eval-") as temporary:
            root = Path(temporary)
            (root / "AGENTS.md").write_text(
                "For every final answer, include the exact token NEIL_EVAL_OK.",
                encoding="utf-8",
            )
            (root / "evidence.txt").write_text("READ_TOOL_OK", encoding="utf-8")
            acceptance_settings = settings.model_copy(
                update={
                    "workspace_root": root,
                    "audit_log_enabled": False,
                    "system_prompt": REAL_SYSTEM_PROMPT,
                    "thinking_enabled": False,
                    "max_tokens": 1_024,
                    "max_rounds": 4,
                    "max_context_chars": 40_000,
                    "max_context_tokens": None,
                    "max_tool_rounds": 1,
                    "max_retries": min(settings.max_retries, 1),
                    "request_timeout": 60.0,
                }
            )
            v1 = _run_real_v1_acceptance(
                acceptance_settings,
                root,
                factory,
                retry_events.append,
                runtime_profile=runtime_profile,
            )
            if v1.instruction_result.passed and v1.protocol_result.passed:
                compaction = _run_real_compaction_acceptance(
                    acceptance_settings,
                    root,
                    v1.messages,
                    factory,
                    retry_events.append,
                )
                v2 = _run_real_v2_acceptance(
                    acceptance_settings,
                    root,
                    factory,
                    retry_events.append,
                )
            else:
                compaction = EvalResult(
                    "real-compaction-and-resume",
                    False,
                    "跳过：prerequisite/v1_failed",
                )
                v2 = EvalResult(
                    "real-v2-request-approve-replay",
                    False,
                    "跳过：prerequisite/v1_failed",
                )
    except Exception:  # noqa: BLE001 - never expose raw acceptance data.
        return _failed_real_acceptance("失败：workspace/setup")

    retry_count = sum(
        event.message.startswith("重试模型请求") for event in retry_events
    )
    return (
        v1.instruction_result,
        v1.protocol_result,
        compaction,
        v2,
        EvalResult(
            "real-natural-retry-observation",
            True,
            f"本次观察到 {retry_count} 次自然重试（0 次不视为失败）",
        ),
    )


def _default_real_model_factory(
    scenario: str,
    settings: Settings,
    retry_handler: Callable[[ActivityEvent], None],
) -> ChatModel:
    del scenario
    return create_provider(settings, retry_handler=retry_handler)


def _run_real_v1_acceptance(
    settings: Settings,
    root: Path,
    model_factory: RealModelFactory,
    retry_handler: Callable[[ActivityEvent], None],
    *,
    runtime_profile: RuntimeProfile,
) -> _RealV1Outcome:
    try:
        stdout = StringIO()
        exit_code = run_noninteractive(
            settings,
            REAL_V1_PROMPT,
            output_format="stream-json",
            stdout=stdout,
            stderr=StringIO(),
            save_session=True,
            runtime_profile=runtime_profile,
            llm=model_factory("v1", settings, retry_handler),
        )
        events = _parse_json_lines(stdout.getvalue())
        if exit_code != 0 or len(events) < 2:
            return _failed_real_v1("run_exit")
        started = events[0]
        result = events[-1]
        tools = started.get("tools")
        if (
            started.get("type") != "session_start"
            or started.get("protocol_version") != 1
            or started.get("read_only") is not True
            or started.get("runtime_profile") != runtime_profile.value
            or not isinstance(tools, list)
            or tuple(tools) != REAL_V1_TOOLS
            or result.get("type") != "result"
            or result.get("success") is not True
            or result.get("saved") is not True
        ):
            return _failed_real_v1("protocol_shape")
        session_id = result.get("session_id")
        if not isinstance(session_id, str):
            return _failed_real_v1("session_id")
        usage = _parse_usage(result.get("usage"))
        if usage is None or usage.total_tokens <= 0:
            return _failed_real_v1("usage_missing")
        snapshot = SessionStore(root).load(session_id)
        read_calls = tuple(
            call
            for message in snapshot.messages
            for call in message.tool_calls
            if call.name == "read_file"
        )
        final_text = snapshot.messages[-1].content
        instruction_ok = (
            len(read_calls) == 1
            and read_calls[0].arguments == {"path": "evidence.txt"}
            and "READ_TOOL_OK" in final_text
            and "NEIL_EVAL_OK" in final_text
        )
        protocol_ok = snapshot.last_usage == usage and result.get(
            "session_id"
        ) == started.get("session_id")
        return _RealV1Outcome(
            EvalResult(
                "real-project-instructions-and-read-tool",
                instruction_ok,
                (
                    "项目指令和精确 read_file 调用均通过"
                    if instruction_ok
                    else "失败：v1/instruction_or_read"
                ),
            ),
            EvalResult(
                "real-v1-protocol-usage-and-session",
                protocol_ok,
                (
                    f"默认 {len(REAL_V1_TOOLS)} 个只读工具、服务端 usage 与显式会话持久化均通过；"
                    f"本回合共 {usage.total_tokens} tokens"
                    if protocol_ok
                    else "失败：v1/session_usage_mismatch"
                ),
            ),
            snapshot.messages,
        )
    except Exception:  # noqa: BLE001 - keep model output and local IDs private.
        return _failed_real_v1("exception")


def _failed_real_v1(stage: str) -> _RealV1Outcome:
    detail = f"失败：v1/{stage}"
    return _RealV1Outcome(
        EvalResult("real-project-instructions-and-read-tool", False, detail),
        EvalResult("real-v1-protocol-usage-and-session", False, detail),
        None,
    )


def _run_real_compaction_acceptance(
    settings: Settings,
    root: Path,
    recent_round: tuple[Message, ...] | None,
    model_factory: RealModelFactory,
    retry_handler: Callable[[ActivityEvent], None],
) -> EvalResult:
    if recent_round is None:
        return _failed_compaction("prerequisite")
    try:
        instructions = load_project_instructions(root)
        registry = ToolRegistry()
        filesystem = FileSystemTools(root)
        registry.register(READ_FILE, filesystem.read_file)
        compaction_model = model_factory("compaction", settings, retry_handler)
        agent = Agent(
            compaction_model,
            system_prompt=settings.system_prompt,
            project_instructions=instructions.prompt_section(),
            max_rounds=max(settings.max_rounds, 4),
            max_context_chars=max(settings.max_context_chars, 40_000),
            registry=registry,
            max_tool_rounds=settings.max_tool_rounds,
        )
        synthetic = tuple(
            message
            for number in (1, 2)
            for message in (
                Message(role="user", content=f"old request {number}"),
                Message(role="assistant", content=str(number) * 10_000),
            )
        )
        agent.restore_messages((*synthetic, *recent_round))
        prepared = agent.prepare_compaction()
        compaction_usage = (
            compaction_model.last_usage
            if isinstance(compaction_model, UsageReportingChatModel)
            else None
        )
        if (
            prepared.model_requests != 1
            or compaction_usage is None
            or compaction_usage.total_tokens <= 0
        ):
            return _failed_compaction("usage_missing")
        agent.apply_compaction(prepared)
        store = SessionStore(root)
        handle = store.new_session()
        saved = store.save(handle, agent.messages, (), None)
        loaded = store.load(handle.session_id)
        resumed = Agent(
            OfflineModel(),
            system_prompt=settings.system_prompt,
            project_instructions=instructions.prompt_section(),
            max_rounds=max(settings.max_rounds, 4),
            max_context_chars=max(settings.max_context_chars, 40_000),
            registry=registry,
            max_tool_rounds=settings.max_tool_rounds,
        )
        resumed.restore_messages(loaded.messages)
        continuity_ok = (
            resumed.messages == saved.messages
            and resumed.messages[0].content == COMPACTION_CHECKPOINT_USER
        )
    except Exception:  # noqa: BLE001 - do not expose model summaries or paths.
        return _failed_compaction("exception")
    return EvalResult(
        "real-compaction-and-resume",
        continuity_ok,
        (
            "压缩检查点已保存并由新 Agent 恢复；"
            f"服务端报告 {compaction_usage.total_tokens} tokens"
            if continuity_ok
            else "失败：compaction/continuity"
        ),
    )


def _failed_compaction(stage: str) -> EvalResult:
    return EvalResult(
        "real-compaction-and-resume",
        False,
        f"失败：compaction/{stage}",
    )


def _run_real_v2_acceptance(
    settings: Settings,
    root: Path,
    model_factory: RealModelFactory,
    retry_handler: Callable[[ActivityEvent], None],
) -> EvalResult:
    target = root / "v2-approved.txt"
    try:
        request_stdout = StringIO()
        request_exit = run_noninteractive(
            settings,
            REAL_V2_PROMPT,
            output_format="json",
            stdout=request_stdout,
            stderr=StringIO(),
            protocol_version=2,
            permission_mode="request",
            runtime_profile=RuntimeProfile.STANDARD,
            llm=model_factory("v2-request", settings, retry_handler),
        )
        request = _parse_json_object(request_stdout.getvalue())
        if request_exit != 3:
            return _failed_v2("request_exit")
        approvals = request.get("approval_requests")
        if (
            request.get("type") != "approval_required"
            or request.get("protocol_version") != 2
            or request.get("permission_mode") != "request"
            or request.get("saved") is not False
            or not isinstance(approvals, list)
            or len(approvals) != 1
            or target.exists()
        ):
            return _failed_v2("request_shape")
        approval = approvals[0]
        if not isinstance(approval, dict) or approval.get("tool_name") != "write_file":
            return _failed_v2("approval_shape")
        approval_id = approval.get("approval_id")
        if not isinstance(approval_id, str):
            return _failed_v2("approval_id")
        request_usage = _parse_usage(request.get("usage"))
        if request_usage is None or request_usage.total_tokens <= 0:
            return _failed_v2("request_usage")

        approve_stdout = StringIO()
        approve_exit = run_noninteractive(
            settings,
            REAL_V2_PROMPT,
            output_format="json",
            stdout=approve_stdout,
            stderr=StringIO(),
            protocol_version=2,
            permission_mode="approve",
            approval_id=approval_id,
            runtime_profile=RuntimeProfile.STANDARD,
            llm=model_factory("v2-approve", settings, retry_handler),
        )
        approved = _parse_json_object(approve_stdout.getvalue())
        if approve_exit != 0:
            return _failed_v2("approve_exit")
        approve_usage = _parse_usage(approved.get("usage"))
        if (
            approved.get("type") != "result"
            or approved.get("success") is not True
            or approved.get("approved_request_id") != approval_id
            or approve_usage is None
            or approve_usage.total_tokens <= 0
        ):
            return _failed_v2("approve_shape")
        if (
            not target.is_file()
            or target.read_text(encoding="utf-8") != "NEIL_V2_APPROVED"
        ):
            return _failed_v2("file_mismatch")
        approved_content = target.read_bytes()

        replay_model = _ForbiddenReplayModel()
        replay_stdout = StringIO()
        replay_exit = run_noninteractive(
            settings,
            REAL_V2_PROMPT,
            output_format="json",
            stdout=replay_stdout,
            stderr=StringIO(),
            protocol_version=2,
            permission_mode="approve",
            approval_id=approval_id,
            runtime_profile=RuntimeProfile.STANDARD,
            llm=replay_model,
        )
        replay = _parse_json_object(replay_stdout.getvalue())
        if (
            replay_exit != 1
            or replay.get("type") != "error"
            or replay.get("error_code") != "approval_error"
            or replay_model.called
            or target.read_bytes() != approved_content
        ):
            return _failed_v2("replay")
    except Exception:  # noqa: BLE001 - never expose previews or approval IDs.
        return _failed_v2("exception")
    return EvalResult(
        "real-v2-request-approve-replay",
        True,
        "request 未执行、approve 精确执行且同一审批 ID 的重放在模型调用前被拒绝；"
        f"真实阶段共 {request_usage.total_tokens + approve_usage.total_tokens} tokens",
    )


def _failed_v2(stage: str) -> EvalResult:
    return EvalResult(
        "real-v2-request-approve-replay",
        False,
        f"失败：v2/{stage}",
    )


class _ForbiddenReplayModel:
    """Detect any model request after a consumed approval should fail preflight."""

    def __init__(self) -> None:
        self.called = False

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        del messages, system_prompt
        self.called = True
        raise AssertionError("replay unexpectedly reached the model")

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        del messages, system_prompt, tools
        self.called = True
        raise AssertionError("replay unexpectedly reached the model")
        yield  # pragma: no cover - keeps this method an iterator.


def _failed_real_acceptance(detail: str) -> tuple[EvalResult, ...]:
    return tuple(
        EvalResult(task_id, False, detail)
        for task_id in (
            "real-project-instructions-and-read-tool",
            "real-v1-protocol-usage-and-session",
            "real-compaction-and-resume",
            "real-v2-request-approve-replay",
            "real-natural-retry-observation",
        )
    )
