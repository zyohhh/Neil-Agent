"""Bounded coding-task evaluations, judged by files and external functional tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..agent import ChatModel
from ..config import Settings
from ..errors import AgentError, NeilAgentError
from ..execution_budget import check_execution_budget, execution_budget_scope
from ..host_runtime import HostMode, RuntimeProfile, build_agent, build_host_runtime
from ..providers.factory import create_provider
from ..schemas import (
    ActivityEvent,
    Message,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from ..session import SessionStore
from .catalog import _select_task_ids
from .coding_catalog import CodingTask, load_coding_tasks
from .grading import grade_task, grader_passed, materialize_task, workspace_delta
from .common import runtime_provenance

CodingModelFactory = Callable[
    [CodingTask, Settings, Callable[[ActivityEvent], None]], ChatModel
]
CodingStatus = Literal[
    "passed",
    "grade_failed",
    "model_failed",
    "budget_exceeded",
    "workspace_changed",
    "setup_failed",
]


class CodingResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    task_sha256: str
    trial: int
    status: CodingStatus
    duration_ms: int
    baseline_failed: bool = False
    grader_exit_code: int | None = None
    model_requests: int = 0
    tool_calls: int = 0
    approved_writes: int = 0
    denied_writes: int = 0
    retry_count: int = 0
    changed_files: tuple[str, ...] = ()
    usage: TokenUsage | None = None
    usage_complete: bool = False


class CodingReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    mode: Literal["real", "simulated"]
    provider: str
    model: str
    agent_version: str
    harness_sha256: str
    python: str
    sdk_versions: dict[str, str]
    runtime_profile: Literal["benchmark-minimal"] = "benchmark-minimal"
    task_count: int
    trials_per_task: int
    max_tool_rounds: int = 6
    max_output_tokens_per_request: int = 2_048
    reported_token_stop_threshold: int = 30_000
    max_model_requests_per_trial: int = 7
    max_tool_calls_per_trial: int = 32
    results: tuple[CodingResult, ...]

    @property
    def success(self) -> bool:
        return bool(self.results) and all(
            result.status == "passed" for result in self.results
        )

    def summary(self) -> dict[str, object]:
        payload = self.model_dump(mode="json")
        passed = sum(result.status == "passed" for result in self.results)
        payload.update(
            success=self.success,
            passed=passed,
            total=len(self.results),
            pass_rate=passed / len(self.results) if self.results else 0.0,
        )
        usage: TokenUsage | None = None
        for result in self.results:
            if result.usage is not None:
                usage = result.usage if usage is None else usage.combine(result.usage)
        payload.update(
            reported_usage=None
            if usage is None
            else {**usage.model_dump(), "total_tokens": usage.total_tokens},
            usage_complete=bool(self.results)
            and all(result.usage_complete for result in self.results),
        )
        return payload


class _MeasuredModel:
    def __init__(self, model: ChatModel) -> None:
        self.model = model
        self.requests = 0
        self.tool_calls = 0
        self.tool_rounds = 0
        self.usage: TokenUsage | None = None
        self.usage_complete = True

    def complete(self, messages: Sequence[Message], *, system_prompt: str) -> str:
        raise AgentError("Coding evaluations do not use non-streaming requests")

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        check_execution_budget()
        if (
            self.requests >= 7
            or self.usage is not None
            and self.usage.total_tokens >= 30_000
        ):
            raise _CodingBudgetExceeded("Coding evaluation budget exceeded")
        self.requests += 1
        response_seen = False
        try:
            for event in self.model.stream(
                messages, system_prompt=system_prompt, tools=tools
            ):
                if isinstance(event, ModelResponse):
                    if response_seen:
                        raise AgentError("Multiple terminal model responses")
                    response_seen = True
                    self.tool_calls += len(event.tool_calls)
                    if event.tool_calls:
                        self.tool_rounds += 1
                    if event.usage is None:
                        self.usage_complete = False
                    else:
                        self.usage = (
                            event.usage
                            if self.usage is None
                            else self.usage.combine(event.usage)
                        )
                    if self.tool_calls > 32 or self.tool_rounds > 6:
                        raise _CodingBudgetExceeded("Coding evaluation budget exceeded")
                yield event
        except BaseException:
            self.usage_complete = False
            raise
        finally:
            if not response_seen:
                self.usage_complete = False


class _ReferenceModel:
    """Deterministic oracle only for testing the harness, never a model score."""

    def __init__(self, task: CodingTask) -> None:
        self.task = task
        self.requests = 0

    def complete(self, messages: Sequence[Message], *, system_prompt: str) -> str:
        raise AssertionError("Reference fixture uses stream only")

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self.requests += 1
        if self.requests == 1:
            yield ModelResponse(
                tool_calls=tuple(
                    ToolCall(
                        id=f"read-{index}", name="read_file", arguments={"path": name}
                    )
                    for index, name in enumerate(self.task.editable_files)
                )
            )
        elif self.requests == 2:
            yield ModelResponse(
                tool_calls=tuple(
                    ToolCall(
                        id=f"replace-{index}",
                        name="replace_text",
                        arguments={
                            "path": name,
                            "old_text": self.task.files[name],
                            "new_text": content,
                        },
                    )
                    for index, (name, content) in enumerate(
                        self.task.reference_files.items()
                    )
                )
            )
        else:
            yield "Fixture patch applied."
            yield ModelResponse(content="Fixture patch applied.")


class _CodingBudgetExceeded(AgentError):
    """A coding-harness request or tool limit stopped a trial."""


def _default_factory(
    task: CodingTask, settings: Settings, activity: Callable[[ActivityEvent], None]
) -> ChatModel:
    return create_provider(settings, retry_handler=activity)


def run_coding_evals(
    settings: Settings,
    *,
    task_ids: Sequence[str] | None = None,
    trials: int = 1,
    simulated: bool = False,
    model_factory: CodingModelFactory | None = None,
) -> CodingReport:
    if not 1 <= trials <= 3:
        raise ValueError("trials must be between 1 and 3")
    tasks = load_coding_tasks()
    selected = _select_task_ids(tuple(task.id for task in tasks), task_ids)
    by_id = {task.id: task for task in tasks}
    factory = model_factory or (
        _default_factory if not simulated else lambda task, *_: _ReferenceModel(task)
    )
    results = tuple(
        _run_trial(by_id[task_id], settings, trial, factory)
        for task_id in selected
        for trial in range(1, trials + 1)
    )
    return CodingReport(
        mode="simulated" if simulated else "real",
        provider="fixture" if simulated else settings.llm_provider.value,
        model="reference-patch" if simulated else settings.selected_model,
        task_count=len(selected),
        trials_per_task=trials,
        results=results,
        **runtime_provenance(),
    )


def _run_trial(
    task: CodingTask, settings: Settings, trial: int, factory: CodingModelFactory
) -> CodingResult:
    started = monotonic()
    measured: _MeasuredModel | None = None
    approved = denied = retries = 0
    status: CodingStatus = "setup_failed"
    deadline = monotonic() + 120.0
    baseline_failed = False
    grader_exit: int | None = None
    changed: tuple[str, ...] = ()

    def activity(event: ActivityEvent) -> None:
        nonlocal retries
        if event.message.startswith("重试模型请求"):
            retries += 1

    try:
        with TemporaryDirectory(prefix="neil-coding-eval-") as temporary:
            control = Path(temporary).resolve()
            workspace = control / "workspace"
            materialize_task(task, workspace)
            baseline = grade_task(task, workspace, control)
            baseline_failed = (
                baseline.reason == "completed" and baseline.returncode not in {None, 0}
            )
            if not baseline_failed:
                raise ValueError("Invalid baseline fixture")
            bounded = settings.model_copy(
                update={
                    "workspace_root": workspace,
                    "system_prompt": "Solve the requested coding task using the available tools. Follow AGENTS.md. The external grader runs after your final response; never claim it already ran.",
                    "thinking_enabled": False,
                    "audit_log_enabled": False,
                    "max_tokens": 2_048,
                    "max_context_chars": 40_000,
                    "max_context_tokens": 12_000,
                    "max_tool_rounds": 6,
                    "max_rounds": 4,
                    "max_retries": min(settings.max_retries, 1),
                    "request_timeout": min(settings.request_timeout, 30.0),
                }
            )
            runtime = build_host_runtime(
                bounded,
                mode=HostMode.NONINTERACTIVE_WRITE,
                profile=RuntimeProfile.BENCHMARK_MINIMAL,
            )
            try:
                measured = _MeasuredModel(factory(task, bounded, activity))
                allowed = {workspace / name for name in task.editable_files}

                def approve(call: ToolCall, preview: str) -> bool:
                    nonlocal approved, denied
                    name = call.arguments.get("path")
                    target = workspace / name if isinstance(name, str) else None
                    accepted = (
                        call.name == "replace_text"
                        and target is not None
                        and target in allowed
                        and target.resolve(strict=True) == target
                    )
                    if accepted:
                        approved += 1
                    else:
                        denied += 1
                    return accepted

                agent = build_agent(
                    bounded,
                    runtime,
                    measured,
                    approval_handler=approve,
                    activity_handler=activity,
                )
                session = SessionStore(workspace).new_session()
                agent.bind_session(session.session_id)
                status = "model_failed"
                with execution_budget_scope(deadline=deadline):
                    for _ in agent.stream_chat(task.prompt):
                        pass
                changed, intact = workspace_delta(task, workspace)
                if not intact:
                    status = "workspace_changed"
                else:
                    grade = grade_task(task, workspace, control)
                    grader_exit = grade.returncode
                    status = (
                        "passed" if changed and grader_passed(grade) else "grade_failed"
                    )
                    agent.mark_execution("acknowledged")
            finally:
                changed, intact = workspace_delta(task, workspace)
                if not intact:
                    status = "workspace_changed"
                runtime.close()
    except NeilAgentError as error:
        if isinstance(error, _CodingBudgetExceeded) or monotonic() >= deadline:
            status = "budget_exceeded"
    except Exception:
        # No raw model output, exception bodies, host paths or prompts in reports.
        if status != "model_failed":
            status = "setup_failed"
    return CodingResult(
        task_id=task.id,
        task_sha256=task.digest,
        trial=trial,
        status=status,
        duration_ms=max(round((monotonic() - started) * 1000), 0),
        baseline_failed=baseline_failed,
        grader_exit_code=grader_exit,
        model_requests=0 if measured is None else measured.requests,
        tool_calls=0 if measured is None else measured.tool_calls,
        approved_writes=approved,
        denied_writes=denied,
        retry_count=retries,
        changed_files=changed,
        usage=None if measured is None else measured.usage,
        usage_complete=measured is not None
        and measured.usage_complete
        and measured.requests > 0,
    )
