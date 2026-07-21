"""Explicit offline and opt-in real DeepSeek acceptance evaluations."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast

import httpx
from anthropic import APIConnectionError, Anthropic
from rich.console import Console

from .agent import Agent, COMPACTION_CHECKPOINT_USER
from .config import Settings, get_settings
from .errors import AgentError, NeilAgentError
from .instructions import (
    MAX_INSTRUCTIONS_FILE_BYTES,
    load_project_instructions,
)
from .llm import LLMClient
from .schemas import ActivityEvent, Message, ModelResponse, ToolCall, ToolDefinition
from .session import SessionStore
from .tools import FileSystemTools, ToolRegistry
from .tools.filesystem import READ_FILE

DEFAULT_TASKS_PATH = Path(__file__).resolve().parents[2] / "evals" / "tasks.json"


@dataclass(frozen=True, slots=True)
class EvalResult:
    """One bounded evaluation outcome suitable for terminal reporting."""

    task_id: str
    passed: bool
    detail: str


class OfflineModel:
    """Deterministic in-process model used by offline evaluations."""

    def __init__(self, response: str = "offline response") -> None:
        self.response = response
        self.system_prompts: list[str] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        self.system_prompts.append(system_prompt)
        return self.response

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self.system_prompts.append(system_prompt)
        yield self.response
        yield ModelResponse(content=self.response)


class FailingCompactionModel(OfflineModel):
    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        raise AgentError("simulated compaction failure")


class ApprovalWorkflowModel(OfflineModel):
    """Request a guarded write and then finish after its denied result."""

    def __init__(self) -> None:
        super().__init__("workflow complete")
        self.calls = 0

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self.calls += 1
        if self.calls == 1:
            yield ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="offline-write",
                        name="write_file",
                        arguments={"path": "result.txt", "content": "blocked"},
                    ),
                )
            )
            return
        yield self.response
        yield ModelResponse(content=self.response)


def run_offline_evals(tasks_path: Path = DEFAULT_TASKS_PATH) -> tuple[EvalResult, ...]:
    """Run every declared task using fake models and temporary workspaces."""

    task_ids = _load_task_ids(tasks_path)
    evaluators: dict[str, Callable[[], str]] = {
        "root-project-instructions": _eval_root_instructions,
        "unsafe-project-instructions": _eval_unsafe_instructions,
        "explicit-context-compaction": _eval_compaction,
        "compaction-failure-atomicity": _eval_compaction_failure,
        "retry-approval-session-consistency": _eval_workflow_consistency,
    }
    results = []
    for task_id in task_ids:
        evaluator = evaluators.get(task_id)
        if evaluator is None:
            results.append(EvalResult(task_id, False, "没有对应的离线执行器"))
            continue
        try:
            detail = evaluator()
        except Exception as error:  # noqa: BLE001 - eval boundary reports failures.
            results.append(
                EvalResult(task_id, False, f"{type(error).__name__}: {error}")
            )
        else:
            results.append(EvalResult(task_id, True, detail))
    return tuple(results)


def run_real_deepseek_acceptance(settings: Settings) -> tuple[EvalResult, ...]:
    """Run a small, read-only real-model acceptance flow after explicit opt-in."""

    retry_events: list[ActivityEvent] = []
    with TemporaryDirectory(prefix="neil-agent-real-eval-") as temporary:
        root = Path(temporary)
        (root / "AGENTS.md").write_text(
            "For every final answer, include the exact token NEIL_EVAL_OK.",
            encoding="utf-8",
        )
        (root / "evidence.txt").write_text("READ_TOOL_OK", encoding="utf-8")
        instructions = load_project_instructions(root)
        registry = ToolRegistry()
        filesystem = FileSystemTools(root)
        registry.register(READ_FILE, filesystem.read_file)
        client = LLMClient(settings, retry_handler=retry_events.append)
        agent = Agent(
            client,
            system_prompt=settings.system_prompt,
            project_instructions=instructions.prompt_section(),
            max_rounds=max(settings.max_rounds, 4),
            max_context_chars=max(settings.max_context_chars, 40_000),
            registry=registry,
            max_tool_rounds=settings.max_tool_rounds,
        )

        response = "".join(
            agent.stream_chat(
                "Use read_file to inspect evidence.txt, then include its exact token "
                "and the project verification token in the final answer."
            )
        )
        used_read_tool = any(
            call.name == "read_file"
            for message in agent.messages
            for call in message.tool_calls
        )
        instruction_ok = "NEIL_EVAL_OK" in response
        read_ok = "READ_TOOL_OK" in response and used_read_tool
        model_detail = (
            f"指令={'通过' if instruction_ok else '失败'}，"
            f"只读工具={'通过' if read_ok else '失败'}"
        )

        recent_round = agent.messages
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
        agent.apply_compaction(prepared)
        store = SessionStore(root)
        handle = store.new_session()
        saved = store.save(handle, agent.messages, (), None)
        loaded = store.load(handle.session_id)
        resumed = Agent(
            client,
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

    retry_count = sum(
        event.message.startswith("重试模型请求") for event in retry_events
    )
    return (
        EvalResult(
            "real-project-instructions-and-read-tool",
            instruction_ok and read_ok,
            model_detail,
        ),
        EvalResult(
            "real-compaction-and-resume",
            continuity_ok,
            "压缩检查点已保存并由新 Agent 恢复" if continuity_ok else "连续性检查失败",
        ),
        EvalResult(
            "real-natural-retry-observation",
            True,
            f"本次观察到 {retry_count} 次自然重试（0 次不视为失败）",
        ),
    )


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
        retry_model = LLMClient(
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


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise AssertionError(detail)


def _load_task_ids(path: Path) -> tuple[str, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取评测任务：{path}") from error
    if not isinstance(payload, list):
        raise ValueError("评测任务根节点必须是数组")
    task_ids = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("每个评测任务都必须包含字符串 id")
        task_ids.append(item["id"])
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("评测任务 id 不能重复")
    return tuple(task_ids)


def _show_results(console: Console, title: str, results: Sequence[EvalResult]) -> int:
    console.print(title, style="bold", markup=False, highlight=False)
    for result in results:
        marker = "[ok]" if result.passed else "[!]"
        style = "green" if result.passed else "red"
        console.print(
            f"{marker} {result.task_id}: {result.detail}",
            style=style,
            markup=False,
            highlight=False,
        )
    passed = sum(result.passed for result in results)
    console.print(f"结果：{passed}/{len(results)} 通过", markup=False, highlight=False)
    return 0 if passed == len(results) else 1


def main() -> None:
    """Run evaluations; real API calls require two explicit command flags."""

    parser = argparse.ArgumentParser(description="Run Neil Agent evaluations.")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS_PATH)
    parser.add_argument("--real-deepseek", action="store_true")
    parser.add_argument("--confirm-api-cost", action="store_true")
    arguments = parser.parse_args()
    console = Console()

    if arguments.real_deepseek:
        if not arguments.confirm_api_cost:
            console.print(
                "真实 DeepSeek 验收会消耗 API 额度；请同时提供 --confirm-api-cost。",
                style="red",
                markup=False,
            )
            raise SystemExit(2)
        try:
            results = run_real_deepseek_acceptance(get_settings())
        except (NeilAgentError, ValueError) as error:
            console.print(f"真实验收失败：{error}", style="red", markup=False)
            raise SystemExit(1) from None
        raise SystemExit(_show_results(console, "真实 DeepSeek 验收", results))

    try:
        results = run_offline_evals(arguments.tasks)
    except ValueError as error:
        console.print(str(error), style="red", markup=False)
        raise SystemExit(2) from None
    raise SystemExit(_show_results(console, "Neil Agent 离线评测", results))
