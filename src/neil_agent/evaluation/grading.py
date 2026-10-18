"""Fixed host graders kept outside the model's editable task workspace."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from ..processes import ProcessResult, run_bounded
from .coding_catalog import CodingTask


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
    """Execute the shipped grader with no inherited secrets or third-party deps."""

    grader = control / "grade.py"
    grader.write_text(task.grader, encoding="utf-8")
    return run_bounded(
        (sys.executable, "-I", "-S", "-B", str(grader), str(root)),
        cwd=control,
        timeout=10.0,
    )


def grader_passed(result: ProcessResult) -> bool:
    return (
        result.reason == "completed"
        and result.returncode == 0
        and result.output.rstrip().endswith("NEIL_GRADER_COMPLETED")
    )
