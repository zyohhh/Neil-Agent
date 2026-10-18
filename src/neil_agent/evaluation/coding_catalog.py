"""Strict definitions for the trusted built-in coding task corpus."""

from __future__ import annotations

from hashlib import sha256
from pathlib import PurePosixPath
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from ..sensitive_paths import is_sensitive_relative_path
from .catalog import TASK_ID_PATTERN, read_catalog


class CodingTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=TASK_ID_PATTERN)
    title: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=8_000)
    files: dict[str, str]
    editable_files: tuple[str, ...] = Field(min_length=1, max_length=8)
    reference_files: dict[str, str]
    grader: str = Field(min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def validate_workspace(self) -> Self:
        if not 1 <= len(self.files) <= 8 or sum(map(len, self.files.values())) > 40_000:
            raise ValueError("coding fixture exceeds workspace limits")
        for name in self.files:
            path = PurePosixPath(name)
            if (
                not path.parts
                or path.suffix != ".py"
                or path.is_absolute()
                or path.as_posix() != name
                or any(part in {".", ".."} for part in path.parts)
                or ":" in name
                or "\\" in name
                or any(ord(char) < 32 for char in name)
                or is_sensitive_relative_path(path.parts)
            ):
                raise ValueError("unsafe coding fixture path")
        editable = set(self.editable_files)
        if (
            len(editable) != len(self.editable_files)
            or not editable <= self.files.keys()
        ):
            raise ValueError("editable files must be unique existing fixture paths")
        if not self.reference_files.keys() <= editable:
            raise ValueError("reference patch exceeds editable files")
        if sum(map(len, self.reference_files.values())) > 40_000:
            raise ValueError("reference patch exceeds limits")
        return self

    @property
    def digest(self) -> str:
        return sha256(self.model_dump_json().encode("utf-8")).hexdigest()


def load_coding_tasks() -> tuple[CodingTask, ...]:
    tasks = TypeAdapter(tuple[CodingTask, ...]).validate_python(
        read_catalog("coding_tasks.json")
    )
    if not 1 <= len(tasks) <= 20 or len({task.id for task in tasks}) != len(tasks):
        raise ValueError("coding task corpus must be nonempty, bounded and unique")
    return tasks
