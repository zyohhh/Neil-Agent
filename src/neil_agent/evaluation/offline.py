"""Offline protocol and runtime invariants; never calls a remote model."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace
from typing import cast

import httpx
from anthropic import APIConnectionError, Anthropic

from ..agent import (
    Agent,
    COMPACTION_CHECKPOINT_USER,
)
from ..config import Settings
from ..errors import AgentError
from ..instructions import (
    MAX_INSTRUCTIONS_FILE_BYTES,
    load_project_instructions,
)
from ..providers.deepseek import DeepSeekProvider
from ..schemas import (
    ActivityEvent,
    Message,
    ToolDefinition,
)
from ..session import SessionStore
from ..host_runtime import (
    HostMode,
    RuntimeProfile,
    build_host_runtime,
)
from ..tools import ToolRegistry


from .catalog import _load_task_ids, _select_task_ids
from .common import EvalResult, _require, _elapsed_ms, _parse_json_object
from .fixtures import OfflineModel, FailingCompactionModel, ApprovalWorkflowModel


def run_offline_evals(
    tasks_path: Path | None = None,
    *,
    task_ids: Sequence[str] | None = None,
    runtime_profile: RuntimeProfile = RuntimeProfile.BENCHMARK_MINIMAL,
    clock: Callable[[], float] = perf_counter,
) -> tuple[EvalResult, ...]:
    """Run every declared task using fake models and temporary workspaces."""

    declared_task_ids = _load_task_ids(tasks_path)
    selected_task_ids = _select_task_ids(declared_task_ids, task_ids)
    evaluators: dict[str, Callable[[], str]] = {
        "root-project-instructions": _eval_root_instructions,
        "unsafe-project-instructions": _eval_unsafe_instructions,
        "explicit-context-compaction": _eval_compaction,
        "compaction-failure-atomicity": _eval_compaction_failure,
        "retry-approval-session-consistency": _eval_workflow_consistency,
        "guest-export-import-approval": _eval_guest_export_import_approval,
    }
    results = []
    for task_id in selected_task_ids:
        started_at = clock()
        evaluator = evaluators.get(task_id)
        if evaluator is None:
            results.append(
                EvalResult(
                    task_id,
                    False,
                    "没有对应的离线执行器",
                    _elapsed_ms(started_at, clock()),
                )
            )
            continue
        try:
            detail = evaluator()
        except Exception as error:  # noqa: BLE001 - eval boundary reports failures.
            results.append(
                EvalResult(
                    task_id,
                    False,
                    f"{type(error).__name__}: {error}",
                    _elapsed_ms(started_at, clock()),
                )
            )
        else:
            results.append(
                EvalResult(
                    task_id,
                    True,
                    detail,
                    _elapsed_ms(started_at, clock()),
                )
            )
    return tuple(results)


def _eval_root_instructions() -> str:
    with TemporaryDirectory(prefix="neil-agent-eval-") as temporary:
        root = Path(temporary)
        secret_rule = "PRIVATE-EVAL-RULE: always add tests"
        (root / "AGENTS.md").write_text(secret_rule, encoding="utf-8")
        instructions = load_project_instructions(root)
        model = OfflineModel()
        agent = Agent(model, project_instructions=instructions.prompt_section())
        agent.chat("change behavior")
        store = SessionStore(root)
        handle = store.new_session()
        store.save(handle, agent.messages, (), None)
        payload = (store.root / f"{handle.session_id}.json").read_text(encoding="utf-8")
        _require(instructions.active, "root instructions were not active")
        _require(
            secret_rule in model.system_prompts[0],
            "root instructions did not reach the system prompt",
        )
        _require(secret_rule not in payload, "instructions leaked into session JSON")
    return "指令已注入系统上下文且未写入会话"


def _eval_unsafe_instructions() -> str:
    with TemporaryDirectory(prefix="neil-agent-eval-") as temporary:
        root = Path(temporary)
        (root / "AGENTS.md").write_bytes(b"x" * (MAX_INSTRUCTIONS_FILE_BYTES + 1))
        instructions = load_project_instructions(root)
        _require(instructions.status == "invalid", "oversized instructions were active")
        _require(
            not instructions.prompt_section(), "unsafe prompt content was generated"
        )
    return "超限指令被拒绝且未生成提示词"


def _eval_compaction() -> str:
    model = OfflineModel("durable offline summary")
    agent = Agent(model)
    history = tuple(
        message
        for number in range(1, 5)
        for message in (
            Message(role="user", content=f"request {number}"),
            Message(
                role="assistant",
                content=(str(number) * 1_000 if number < 3 else f"answer {number}"),
            ),
        )
    )
    agent.restore_messages(history)
    prepared = agent.prepare_compaction()
    agent.apply_compaction(prepared)
    _require(
        agent.messages[0].content == COMPACTION_CHECKPOINT_USER,
        "compaction checkpoint is missing",
    )
    _require(agent.messages[-4:] == history[-4:], "recent rounds changed")
    return "较早轮次已压缩，最近两轮保持完整"


def _eval_compaction_failure() -> str:
    agent = Agent(FailingCompactionModel())
    history = tuple(
        message
        for number in range(1, 4)
        for message in (
            Message(role="user", content=f"request {number}"),
            Message(role="assistant", content=str(number) * 1_000),
        )
    )
    agent.restore_messages(history)
    try:
        agent.prepare_compaction()
    except AgentError:
        pass
    else:
        raise AssertionError("compaction failure was not propagated")
    _require(agent.messages == history, "failed compaction changed history")
    return "摘要失败后内存历史保持不变"


def _eval_workflow_consistency() -> str:
    with TemporaryDirectory(prefix="neil-agent-eval-") as temporary:
        root = Path(temporary)
        retry_activities: list[ActivityEvent] = []
        retry_delays: list[float] = []
        request = httpx.Request("POST", "https://api.deepseek.com/messages")

        class RetryMessages:
            def __init__(self) -> None:
                self.calls = 0

            def create(self, **kwargs: object) -> object:
                self.calls += 1
                if self.calls == 1:
                    raise APIConnectionError(request=request)
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="recovered")]
                )

        retry_messages = RetryMessages()
        retry_client = SimpleNamespace(messages=retry_messages)
        retry_settings = Settings.model_validate(
            {
                "deepseek_api_key": "offline-key",
                "max_retries": 1,
                "retry_base_delay": 0,
            }
        )
        retry_model = DeepSeekProvider(
            retry_settings,
            client=cast(Anthropic, retry_client),
            retry_handler=retry_activities.append,
            sleeper=retry_delays.append,
        )
        recovered = retry_model.complete(
            (Message(role="user", content="offline retry"),),
            system_prompt="Offline evaluation.",
        )
        _require(recovered == "recovered", "transient request did not recover")
        _require(retry_messages.calls == 2, "retry attempt count was not two")
        _require(retry_delays == [0.0], "retry delay was not bounded and observable")
        _require(
            [event.message for event in retry_activities]
            == ["模型请求暂时失败，等待重试", "重试模型请求"],
            "retry activities were incomplete",
        )

        writes: list[tuple[str, str]] = []

        def record_write(path: str, content: str) -> str:
            writes.append((path, content))
            return "written"

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="write_file",
                description="Offline approval test.",
                input_schema={"type": "object"},
            ),
            record_write,
            requires_approval=True,
            preview_handler=lambda path, content: f"write {path}: {content}",
        )
        model = ApprovalWorkflowModel()
        agent = Agent(
            model, registry=registry, approval_handler=lambda call, preview: False
        )
        response = "".join(agent.stream_chat("attempt a guarded write"))
        _require(response == "workflow complete", "tool workflow did not finish")
        _require(writes == [], "denied write was executed")
        _require(
            any(
                "用户拒绝" in result.content
                for message in agent.messages
                for result in message.tool_results
            ),
            "denied tool result was not recorded",
        )
        store = SessionStore(root)
        handle = store.new_session()
        saved = store.save(handle, agent.messages, (), None)
        _require(
            store.load(handle.session_id).messages == saved.messages,
            "saved tool round could not be restored",
        )
    return "连接异常真实经过重试；拒绝写入后完整工具轮次可恢复"


def _eval_guest_export_import_approval() -> str:

    from neil_agent.noninteractive import run_noninteractive
    from neil_agent.sandbox_export import build_guest_export_manifest
    from neil_agent.schemas import ModelResponse, ToolCall

    with TemporaryDirectory(prefix="neil-agent-eval-import-") as temporary:
        root = Path(temporary)
        settings = Settings.model_validate(
            {
                "deepseek_api_key": "offline-key",
                "workspace_root": root,
            }
        )
        runtime = build_host_runtime(
            settings,
            mode=HostMode.NONINTERACTIVE_WRITE,
            profile=RuntimeProfile.STANDARD,
        )
        assert runtime.guest_import is not None
        manifest = build_guest_export_manifest(
            run_id="run-eval",
            request_hash="d" * 64,
            certification_sha256="e" * 64,
            files=(("eval.txt", b"EVAL_IMPORT_OK"),),
        )
        digest = runtime.guest_import.stage(manifest, {"eval.txt": b"EVAL_IMPORT_OK"})

        class ImportModel:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, messages, *, system_prompt: str) -> str:
                del messages, system_prompt
                return "done"

            def stream(self, messages, *, system_prompt: str, tools=()):
                del messages, system_prompt, tools
                self.calls += 1
                if self.calls == 1:
                    yield ModelResponse(
                        tool_calls=(
                            ToolCall(
                                id="import-eval",
                                name="import_guest_export",
                                arguments={"manifest_sha256": digest},
                            ),
                        )
                    )
                    return
                yield "import complete"
                yield ModelResponse(content="import complete")

        model = ImportModel()
        request_stdout = StringIO()
        request_exit = run_noninteractive(
            settings,
            "import guest export",
            output_format="json",
            stdout=request_stdout,
            stderr=StringIO(),
            protocol_version=2,
            permission_mode="request",
            llm=model,
        )
        request = _parse_json_object(request_stdout.getvalue())
        _require(request_exit == 3, "request mode did not stop for approval")
        approvals_value = request.get("approval_requests")
        _require(
            isinstance(approvals_value, list) and len(approvals_value) == 1,
            "request mode did not emit one approval",
        )
        assert isinstance(approvals_value, list)
        approval = approvals_value[0]
        _require(
            isinstance(approval, dict)
            and approval.get("tool_name") == "import_guest_export"
            and approval.get("binding_kind") == "guest-export-import",
            "approval targeted the wrong tool or binding",
        )
        approval_id = approval.get("approval_id")
        _require(isinstance(approval_id, str), "approval id missing")

        approve_stdout = StringIO()
        approve_exit = run_noninteractive(
            settings,
            "import guest export",
            output_format="json",
            stdout=approve_stdout,
            stderr=StringIO(),
            protocol_version=2,
            permission_mode="approve",
            approval_id=approval_id,
            llm=ImportModel(),
        )
        approved = _parse_json_object(approve_stdout.getvalue())
        _require(approve_exit == 0, "approve mode failed")
        _require(approved.get("success") is True, "approve result was not successful")
        target = root / "eval.txt"
        _require(
            target.is_file() and target.read_text(encoding="utf-8") == "EVAL_IMPORT_OK",
            "guest export import did not write the staged file",
        )
    return "guest export 二次批准导入在非交互 v2 下完成且绑定正确"
