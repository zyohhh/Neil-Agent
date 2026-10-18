"""Coding scores require real file changes and an independent functional grader."""

import json
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from neil_agent import evals
from neil_agent.config import Settings
from neil_agent.errors import LLMError
from neil_agent.evaluation import coding as coding_module
from neil_agent.evaluation.coding import _ReferenceModel, run_coding_evals
from neil_agent.evaluation.coding_catalog import CodingTask, FunctionCase, load_coding_tasks
from neil_agent.host_runtime import BENCHMARK_MINIMAL_WRITE_TOOLS
from neil_agent.schemas import ModelResponse, TokenUsage, ToolCall


def _settings() -> Settings:
    return Settings.model_construct(deepseek_api_key=SecretStr("private-test-key"))


class NoChangeModel:
    def complete(self, *args, **kwargs):
        raise AssertionError("stream only")

    def stream(self, *args, **kwargs):
        yield "I fixed everything and all tests passed."
        yield ModelResponse(content="I fixed everything and all tests passed.")


def test_reference_fixture_exercises_all_tasks_but_is_not_a_real_score() -> None:
    report = run_coding_evals(_settings(), simulated=True)
    assert report.mode == "simulated" and report.provider == "fixture"
    assert report.success and report.task_count == 3
    assert all(
        result.baseline_failed and result.grader_exit_code == 0
        for result in report.results
    )
    assert [result.approved_writes for result in report.results] == [1, 1, 2]
    assert all(
        result.usage is None and not result.usage_complete for result in report.results
    )
    assert report.summary()["pass_rate"] == 1
    assert len(report.harness_sha256) == 64
    assert set(report.sdk_versions) == {"anthropic", "openai"}
    assert report.summary()["reported_usage"] is None


def test_completion_claim_without_changes_fails_grading() -> None:
    report = run_coding_evals(
        _settings(),
        task_ids=("pagination-boundaries",),
        simulated=True,
        model_factory=lambda *_: NoChangeModel(),
    )
    result = report.results[0]
    assert not report.success and result.status == "grade_failed"
    assert result.changed_files == () and result.grader_exit_code != 0


@pytest.mark.parametrize("source", [
    'print("NEIL_GRADER_COMPLETED")\nraise SystemExit(0)\n',
    'import os\nprint("NEIL_GRADER_COMPLETED", flush=True)\nos._exit(0)\n',
    'print("functional_checks_passed")\nraise SystemExit(0)\n',
    'import os\nos.write(1, b\'{"challenge":"fake","calls":[]}\')\nos._exit(0)\n',
    'def paginate(*args):\n    print("NEIL_GRADER_COMPLETED")\n    return []\n',
])
def test_source_cannot_issue_its_own_passing_receipt(source: str) -> None:
    def factory(task, *_):
        return _ReferenceModel(task.model_copy(update={"reference_files": {"pager.py": source}}))

    result = run_coding_evals(
        _settings(), task_ids=("pagination-boundaries",),
        simulated=True, model_factory=factory,
    ).results[0]
    assert result.baseline_failed and result.changed_files == ("pager.py",)
    assert result.status == "grade_failed"


@pytest.mark.parametrize("kind", ["mutation", "alias"])
def test_parent_grader_checks_mutation_and_nested_aliases(kind: str) -> None:
    def factory(task, *_):
        sources = dict(task.reference_files)
        if kind == "mutation":
            sources["config_reader.py"] = sources["config_reader.py"].replace(
                "result = deepcopy(defaults)", "result = defaults"
            )
        else:
            sources["config_reader.py"] = sources["config_reader.py"].replace(
                "result = deepcopy(defaults)", "result = dict(defaults)"
            )
        return _ReferenceModel(task.model_copy(update={"reference_files": sources}))

    report = run_coding_evals(
        _settings(), task_ids=("config-precedence",), simulated=True, model_factory=factory
    )
    assert report.results[0].status == "grade_failed"


def test_server_usage_and_actual_profile_are_recorded_and_workspaces_deleted() -> None:
    workspaces: list[Path] = []

    class UsageModel(_ReferenceModel):
        def stream(self, messages, *, system_prompt, tools=()):
            assert tuple(tool.name for tool in tools) == BENCHMARK_MINIMAL_WRITE_TOOLS
            for event in super().stream(
                messages, system_prompt=system_prompt, tools=tools
            ):
                if isinstance(event, ModelResponse):
                    event = event.model_copy(
                        update={"usage": TokenUsage(input_tokens=20, output_tokens=3)}
                    )
                yield event

    def factory(task, settings, activity):
        assert settings.max_tokens == 2048 and settings.max_tool_rounds == 6
        assert settings.max_context_tokens == 12000 and settings.max_retries <= 1
        assert not settings.thinking_enabled
        workspaces.append(settings.workspace_root)
        return UsageModel(task)

    report = run_coding_evals(
        _settings(),
        task_ids=("pagination-boundaries",),
        simulated=True,
        model_factory=factory,
    )
    result = report.results[0]
    assert result.status == "passed" and result.model_requests == 3
    assert result.usage_complete and result.usage == TokenUsage(
        input_tokens=60, output_tokens=9
    )
    assert workspaces and all(not path.exists() for path in workspaces)


def test_failed_model_retains_partial_patch_metrics_without_leaking_output() -> None:
    class FailingModel(_ReferenceModel):
        def stream(self, *args, **kwargs):
            if self.requests == 2:
                raise LLMError("PRIVATE-API-KEY and provider body")
            yield from super().stream(*args, **kwargs)

    report = run_coding_evals(
        _settings(),
        task_ids=("pagination-boundaries",),
        simulated=True,
        model_factory=lambda task, *_: FailingModel(task),
    )
    result = report.results[0]
    assert result.status == "model_failed" and result.changed_files == ("pager.py",)
    assert result.approved_writes == 1 and not result.usage_complete
    assert "PRIVATE" not in json.dumps(report.summary())


def test_noneditable_instruction_file_cannot_be_changed() -> None:
    class WrongFileModel(NoChangeModel):
        calls = 0

        def stream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="bad",
                            name="replace_text",
                            arguments={
                                "path": "AGENTS.md",
                                "old_text": "Use only the provided tools.",
                                "new_text": "PRIVATE-OVERRIDE",
                            },
                        ),
                    )
                )
            else:
                yield ModelResponse(content="done")

    report = run_coding_evals(
        _settings(),
        task_ids=("pagination-boundaries",),
        simulated=True,
        model_factory=lambda *_: WrongFileModel(),
    )
    result = report.results[0]
    assert result.status == "grade_failed" and result.denied_writes == 1
    assert result.changed_files == () and "PRIVATE" not in report.model_dump_json()


def test_tool_flood_is_stopped_before_mutation() -> None:
    class FloodModel(NoChangeModel):
        def stream(self, *args, **kwargs):
            yield ModelResponse(
                tool_calls=tuple(
                    ToolCall(
                        id=f"call-{index}",
                        name="replace_text",
                        arguments={
                            "path": "pager.py",
                            "old_text": "start = page * page_size",
                            "new_text": "start = (page - 1) * page_size",
                        },
                    )
                    for index in range(33)
                )
            )

    report = run_coding_evals(
        _settings(),
        task_ids=("pagination-boundaries",),
        simulated=True,
        model_factory=lambda *_: FloodModel(),
    )
    result = report.results[0]
    assert result.status == "budget_exceeded" and result.approved_writes == 0
    assert result.changed_files == () and not result.usage_complete


def test_reported_token_threshold_prevents_next_model_request() -> None:
    class ExpensiveModel(_ReferenceModel):
        def stream(self, *args, **kwargs):
            for event in super().stream(*args, **kwargs):
                if isinstance(event, ModelResponse):
                    event = event.model_copy(
                        update={"usage": TokenUsage(input_tokens=30000)}
                    )
                yield event

    report = run_coding_evals(
        _settings(),
        task_ids=("pagination-boundaries",),
        simulated=True,
        model_factory=lambda task, *_: ExpensiveModel(task),
    )
    result = report.results[0]
    assert result.status == "budget_exceeded" and result.model_requests == 1
    assert result.changed_files == ()
    assert result.usage == TokenUsage(input_tokens=30000)


def test_invalid_baseline_never_constructs_a_paid_model(monkeypatch) -> None:
    task = next(task for task in load_coding_tasks() if task.id == "pagination-boundaries").model_copy(
        update={"cases": (FunctionCase(module="pager", function="paginate", args=([1, 2], 1, 1), expected=[2]),)}
    )
    monkeypatch.setattr(coding_module, "load_coding_tasks", lambda: (task,))

    def forbidden(*_):
        pytest.fail("Invalid fixtures must not create a provider")

    result = run_coding_evals(_settings(), model_factory=forbidden).results[0]
    assert result.status == "setup_failed" and not result.baseline_failed


def test_trials_are_fresh_and_task_selection_is_bounded() -> None:
    report = run_coding_evals(
        _settings(), simulated=True, task_ids=("slug-collision",), trials=2
    )
    assert report.task_count == 1 and report.trials_per_task == 2
    assert [result.trial for result in report.results] == [1, 2]
    assert all(result.status == "passed" for result in report.results)
    with pytest.raises(ValueError):
        run_coding_evals(_settings(), simulated=True, trials=4)
    with pytest.raises(ValueError):
        run_coding_evals(_settings(), simulated=True, task_ids=("unknown",))


@pytest.mark.parametrize(
    "unsafe",
    [
        "../escape.py",
        "/tmp/escape.py",
        ".neil-agent/state.py",
        "C:/escape.py",
        "bad\\file.py",
    ],
)
def test_task_catalog_rejects_unsafe_paths(unsafe: str) -> None:
    task = load_coding_tasks()[0].model_dump()
    task.update(
        files={unsafe: "bad"},
        editable_files=(unsafe,),
        reference_files={unsafe: "fixed"},
    )
    with pytest.raises(ValueError):
        CodingTask.model_validate(task)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--coding-tasks"],
        ["--coding-tasks", "--runtime-profile", "standard", "--confirm-api-cost"],
        ["--simulate"],
        ["--coding-tasks", "--tasks", "external.json", "--confirm-api-cost"],
    ],
)
def test_cli_rejects_unsafe_modes_before_loading_credentials(
    arguments, monkeypatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["neil-agent-eval", *arguments])
    monkeypatch.setattr(
        evals,
        "get_settings",
        lambda: pytest.fail("Credentials loaded before mode validation"),
    )
    with pytest.raises(SystemExit) as result:
        evals.main()
    assert result.value.code == 2


def test_simulated_cli_does_not_read_environment_configuration(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "neil-agent-eval",
            "--coding-tasks",
            "--simulate",
            "--task",
            "pagination-boundaries",
            "--format",
            "json",
        ],
    )
    monkeypatch.setattr(
        evals, "get_settings", lambda: pytest.fail("Simulation loaded real settings")
    )
    monkeypatch.setenv("LLM_PROVIDER", "not-a-provider")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "PRIVATE-ENV-KEY")
    with pytest.raises(SystemExit) as result:
        evals.main()
    assert result.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "simulated" and payload["success"]
    assert "PRIVATE" not in json.dumps(payload)
