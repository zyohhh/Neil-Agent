"""Structural checks for the repository's manual regression scenarios."""

import json
from pathlib import Path


def test_eval_tasks_have_unique_ids_and_actionable_expectations() -> None:
    path = Path(__file__).parents[1] / "evals" / "tasks.json"
    tasks = json.loads(path.read_text(encoding="utf-8"))

    assert len(tasks) >= 5
    assert len({task["id"] for task in tasks}) == len(tasks)
    for task in tasks:
        assert task["capability"]
        assert task["steps"]
        assert task["expected"]
