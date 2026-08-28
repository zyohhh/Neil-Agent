"""Structural and executable checks for repository regression scenarios."""

import json
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from neil_agent import evals as eval_module
from neil_agent.config import Settings
from neil_agent.evals import run_offline_evals
from neil_agent.schemas import (
    ActivityEvent,
    Message,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)


class _FakeRealAcceptanceModel:
    def __init__(
        self,
        scenario: str,
        *,
        valid_v1: bool,
        compaction_usage: bool,
    ) -> None:
        self.scenario = scenario
        self.valid_v1 = valid_v1
        self.compaction_usage = compaction_usage
        self.calls = 0
        self._last_usage: TokenUsage | None = None

    @property
    def last_usage(self) -> TokenUsage | None:
        return self._last_usage

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        del messages, system_prompt
        if self.scenario != "compaction":
            raise AssertionError(f"unexpected complete call for {self.scenario}")
        if self.compaction_usage:
            self._last_usage = TokenUsage(input_tokens=12, output_tokens=3)
        return "Durable compact summary."

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        del messages, system_prompt
        self.calls += 1
        if self.scenario == "v1":
            if not self.valid_v1:
                text = "PRIVATE-RAW-MODEL-OUTPUT"
                yield text
                yield ModelResponse(
                    content=text,
                    usage=TokenUsage(input_tokens=3, output_tokens=1),
                )
                return
            assert tuple(tool.name for tool in tools) == eval_module.REAL_V1_TOOLS
            if self.calls == 1:
                yield ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="real-v1-read",
                            name="read_file",
                            arguments={"path": "evidence.txt"},
                        ),
                    ),
                    usage=TokenUsage(input_tokens=8, output_tokens=2),
                )
                return
            text = "READ_TOOL_OK NEIL_EVAL_OK"
            yield text
            yield ModelResponse(
                content=text,
                usage=TokenUsage(input_tokens=5, output_tokens=2),
            )
            return
        if self.scenario in {"v2-request", "v2-approve"}:
            if self.calls == 1:
                yield ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id=f"{self.scenario}-write",
                            name="write_file",
                            arguments={
                                "path": "v2-approved.txt",
                                "content": "NEIL_V2_APPROVED",
                            },
                        ),
                    ),
                    usage=TokenUsage(input_tokens=9, output_tokens=2),
                )
                return
            text = "NEIL_EVAL_OK"
            yield text
            yield ModelResponse(
                content=text,
                usage=TokenUsage(input_tokens=4, output_tokens=1),
            )
            return
        raise AssertionError(f"unexpected stream call for {self.scenario}")


class _FakeRealModelFactory:
    def __init__(
        self,
        *,
        valid_v1: bool = True,
        compaction_usage: bool = True,
    ) -> None:
        self.valid_v1 = valid_v1
        self.compaction_usage = compaction_usage
        self.scenarios: list[str] = []
        self.settings: list[Settings] = []

    def __call__(
        self,
        scenario: str,
        settings: Settings,
        retry_handler: Callable[[ActivityEvent], None],
    ) -> _FakeRealAcceptanceModel:
        del retry_handler
        self.scenarios.append(scenario)
        self.settings.append(settings)
        return _FakeRealAcceptanceModel(
            scenario,
            valid_v1=self.valid_v1,
            compaction_usage=self.compaction_usage,
        )


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="offline-key",
        thinking_enabled=False,
    )


def test_eval_tasks_have_unique_ids_and_actionable_expectations() -> None:
    path = Path(__file__).parents[1] / "evals" / "tasks.json"
    tasks = json.loads(path.read_text(encoding="utf-8"))

    assert len(tasks) >= 5
    assert len({task["id"] for task in tasks}) == len(tasks)
    for task in tasks:
        assert task["capability"]
        assert task["steps"]
        assert task["expected"]


def test_all_declared_offline_evals_pass_without_network_access() -> None:
    path = Path(__file__).parents[1] / "evals" / "tasks.json"

    results = run_offline_evals(path)

    assert len(results) >= 5
    assert all(result.passed for result in results), results


def test_real_eval_requires_separate_api_cost_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["neil-agent-eval", "--real-deepseek"])
    monkeypatch.setattr(
        eval_module,
        "get_settings",
        lambda: (_ for _ in ()).throw(AssertionError("config must not load")),
    )

    with pytest.raises(SystemExit) as exit_info:
        eval_module.main()

    assert exit_info.value.code == 2


def test_confirm_cost_flag_alone_keeps_the_eval_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["neil-agent-eval", "--confirm-api-cost", "--format", "json"],
    )
    monkeypatch.setattr(
        eval_module,
        "run_offline_evals",
        lambda *args, **kwargs: (
            eval_module.EvalResult("offline-confirm-only", True, "offline"),
        ),
    )
    monkeypatch.setattr(
        eval_module,
        "run_real_deepseek_acceptance",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("real acceptance must remain gated")
        ),
    )

    with pytest.raises(SystemExit) as exit_info:
        eval_module.main()

    assert exit_info.value.code == 0


def test_both_cost_flags_enter_real_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    observed: list[Settings] = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "neil-agent-eval",
            "--real-deepseek",
            "--confirm-api-cost",
            "--format",
            "json",
        ],
    )
    monkeypatch.setattr(eval_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        eval_module,
        "run_real_deepseek_acceptance",
        lambda value, **kwargs: (
            observed.append(value)
            or (eval_module.EvalResult("real-gated", True, "accepted"),)
        ),
    )

    with pytest.raises(SystemExit) as exit_info:
        eval_module.main()

    assert exit_info.value.code == 0
    assert observed == [settings]


def test_real_acceptance_judges_v1_v2_usage_and_session_with_fake_models() -> None:
    factory = _FakeRealModelFactory()

    results = eval_module.run_real_deepseek_acceptance(
        _settings(),
        model_factory=factory,
    )

    assert [result.task_id for result in results] == [
        "real-project-instructions-and-read-tool",
        "real-v1-protocol-usage-and-session",
        "real-compaction-and-resume",
        "real-v2-request-approve-replay",
        "real-natural-retry-observation",
    ]
    assert all(result.passed for result in results), results
    assert factory.scenarios == [
        "v1",
        "compaction",
        "v2-request",
        "v2-approve",
    ]
    assert "tokens" in results[1].detail
    assert "tokens" in results[2].detail
    assert "tokens" in results[3].detail
    assert all(
        settings.system_prompt == eval_module.REAL_SYSTEM_PROMPT
        for settings in factory.settings
    )
    assert all(not settings.thinking_enabled for settings in factory.settings)
    assert all(settings.max_tokens == 1_024 for settings in factory.settings)
    assert all(settings.max_rounds == 4 for settings in factory.settings)
    assert all(settings.max_context_chars == 40_000 for settings in factory.settings)
    assert all(settings.max_context_tokens is None for settings in factory.settings)
    assert all(settings.max_tool_rounds == 1 for settings in factory.settings)
    assert all(settings.max_retries == 1 for settings in factory.settings)
    assert all(settings.request_timeout == 60.0 for settings in factory.settings)
    assert "do not call a tool again" in eval_module.REAL_V2_PROMPT


def test_real_acceptance_failure_does_not_echo_model_output_or_local_ids() -> None:
    factory = _FakeRealModelFactory(valid_v1=False)
    results = eval_module.run_real_deepseek_acceptance(
        _settings(),
        model_factory=factory,
    )

    instruction = results[0]
    assert not instruction.passed
    assert "PRIVATE-RAW-MODEL-OUTPUT" not in instruction.detail
    assert all("approval_id" not in result.detail.lower() for result in results)
    assert factory.scenarios == ["v1"]
    assert results[2].detail == "跳过：prerequisite/v1_failed"
    assert results[3].detail == "跳过：prerequisite/v1_failed"


def test_real_acceptance_requires_server_usage_for_compaction() -> None:
    results = eval_module.run_real_deepseek_acceptance(
        _settings(),
        model_factory=_FakeRealModelFactory(compaction_usage=False),
    )

    assert results[0].passed
    assert results[1].passed
    assert not results[2].passed
    assert results[2].detail == "失败：compaction/usage_missing"
    assert results[3].passed


def test_offline_eval_supports_single_task_and_stable_duration() -> None:
    path = Path(__file__).parents[1] / "evals" / "tasks.json"
    times = iter((10.0, 10.125))

    results = run_offline_evals(
        path,
        task_ids=("root-project-instructions",),
        clock=lambda: next(times),
    )

    assert len(results) == 1
    assert results[0].passed
    assert results[0].duration_ms == 125


def test_offline_eval_rejects_unknown_task() -> None:
    path = Path(__file__).parents[1] / "evals" / "tasks.json"

    with pytest.raises(ValueError, match="未知评测任务"):
        run_offline_evals(path, task_ids=("missing",))
