"""Fixed host graders kept outside the model's editable task workspace."""

from __future__ import annotations

import os
import json
import secrets
import stat
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from ..processes import ProcessResult, run_bounded
from .coding_catalog import CodingTask


class _CallEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: JsonValue
    error: str | None
    args_after: list[JsonValue]
    same_input: bool
    shares_input: bool


class _GradeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    challenge: str
    calls: list[_CallEvidence]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def materialize_task(task: CodingTask, root: Path) -> None:
    root.mkdir()
    for name, content in task.files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (root / "AGENTS.md").write_text(instructions_for(task), encoding="utf-8")


def instructions_for(task: CodingTask) -> str:
    return (
        "Use only the provided tools. Modify only these existing files: "
        + ", ".join(task.editable_files)
        + ". Do not claim that tests ran: an external grader runs after your final answer.\n"
    )


def workspace_delta(task: CodingTask, root: Path) -> tuple[tuple[str, ...], bool]:
    """Inspect permitted source changes before and after executing generated code."""

    changed: list[str] = []
    valid = True
    try:
        for name, original in task.files.items():
            target = root / name
            info = target.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or target.resolve(strict=True) != target
                or info.st_size > 65_536
            ):
                changed.append(name)
                valid = False
            elif target.read_text(encoding="utf-8") != original:
                changed.append(name)
        valid = valid and set(changed) <= set(task.editable_files)
        valid = valid and (root / "AGENTS.md").read_text(
            encoding="utf-8"
        ) == instructions_for(task)
        count = 0
        for directory, folders, names in os.walk(root, followlinks=False):
            if Path(directory) == root:
                folders[:] = [name for name in folders if name != ".neil-agent"]
            for folder in folders:
                if (Path(directory) / folder).is_symlink():
                    return tuple(changed), False
            for name in names:
                count += 1
                relative = (Path(directory) / name).relative_to(root).as_posix()
                if count > 32 or relative not in {*task.files, "AGENTS.md"}:
                    return tuple(changed), False
        return tuple(changed), valid
    except (OSError, UnicodeError):
        return tuple(changed), False


def grade_task(task: CodingTask, root: Path, control: Path) -> ProcessResult:
    """Decide functional assertions here; generated source only runs in a worker."""

    challenge = secrets.token_hex(32)
    request = control / "calls.json"
    request.write_text(json.dumps({
        "challenge": challenge,
        "calls": [case.model_dump(include={"module", "function", "args"}) for case in task.cases],
    }), encoding="utf-8")
    worker = Path(__file__).with_name("grading_worker.py")
    result = run_bounded(
        (sys.executable, "-I", "-S", "-B", str(worker), str(request), str(root)),
        cwd=control,
        timeout=10.0,
        merge_stderr=False,
    )
    if result.reason != "completed" or result.returncode != 0:
        return ProcessResult(result.returncode, "", result.reason if result.reason != "completed" else "worker_failed")
    try:
        evidence = _GradeEvidence.model_validate_json(result.output)
        if evidence.challenge != challenge or len(evidence.calls) != len(task.cases):
            raise ValueError("Incomplete invocation evidence")
        passed = all(
            actual.error == case.raises
            and _canonical(actual.args_after) == _canonical(case.args)
            and (case.raises is not None or _canonical(actual.value) == _canonical(case.expected))
            and (case.identity == "any" or not actual.same_input)
            and (case.identity != "disjoint" or not actual.shares_input)
            for case, actual in zip(task.cases, evidence.calls, strict=True)
        )
    except (ValueError, ValidationError):
        return ProcessResult(1, "", "invalid_evidence")
    return ProcessResult(0 if passed else 1, "functional_checks_passed" if passed else "")


def grader_passed(result: ProcessResult) -> bool:
    return (
        result.reason == "completed"
        and result.returncode == 0
        and result.output == "functional_checks_passed"
    )
