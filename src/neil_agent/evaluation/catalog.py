"""Bounded packaged task catalogs, independent of the repository checkout."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path

DEFAULT_TASKS_PATH = Path(__file__).with_name("tasks.json")
MAX_CATALOG_BYTES = 262_144
TASK_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,79}$"


def read_catalog(name: str, path: Path | None = None) -> object:
    source = path if path is not None else files(__package__).joinpath(name)
    try:
        with source.open("rb") as stream:
            payload = stream.read(MAX_CATALOG_BYTES + 1)
        if len(payload) > MAX_CATALOG_BYTES:
            raise ValueError("评测任务目录超过大小上限")
        return json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("无法读取评测任务目录") from error


def _load_task_ids(path: Path | None = None) -> tuple[str, ...]:
    payload = read_catalog("tasks.json", path)
    if not isinstance(payload, list) or not 1 <= len(payload) <= 100:
        raise ValueError("评测任务根节点必须是非空有界数组")
    task_ids: list[str] = []
    for item in payload:
        task_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(task_id, str) or not re.fullmatch(TASK_ID_PATTERN, task_id):
            raise ValueError("评测任务 id 格式无效")
        task_ids.append(task_id)
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("评测任务 id 不能重复")
    return tuple(task_ids)


def _select_task_ids(
    declared: Sequence[str], requested: Sequence[str] | None
) -> tuple[str, ...]:
    if not requested:
        return tuple(declared)
    if len(requested) != len(set(requested)):
        raise ValueError("--task 不能重复")
    unknown = [task_id for task_id in requested if task_id not in declared]
    if unknown:
        raise ValueError(f"未知评测任务：{', '.join(unknown)}")
    return tuple(requested)
