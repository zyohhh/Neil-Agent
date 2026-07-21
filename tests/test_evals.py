"""Structural and executable checks for repository regression scenarios."""

import json
import sys
from pathlib import Path

import pytest

from neil_agent import evals as eval_module
from neil_agent.evals import run_offline_evals


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

    with pytest.raises(SystemExit) as exit_info:
        eval_module.main()

    assert exit_info.value.code == 2
