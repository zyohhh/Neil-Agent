"""Shared evaluation results and protocol parsing."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version
from importlib.resources import files
from typing import TypedDict


from ..schemas import (
    TokenUsage,
)
from .. import __version__


class RuntimeProvenance(TypedDict):
    agent_version: str
    harness_sha256: str
    python: str
    sdk_versions: dict[str, str]


def runtime_provenance() -> RuntimeProvenance:
    """Fingerprint the harness and installed SDKs without reading configuration."""

    root = files("neil_agent")
    names = [
        "agent.py",
        "host_runtime.py",
        "execution_budget.py",
        "instructions.py",
        "processes.py",
        "tools/filesystem.py",
        "tools/registry.py",
        "evaluation/coding.py",
        "evaluation/grading.py",
    ]
    names.extend(
        f"providers/{entry.name}"
        for entry in root.joinpath("providers").iterdir()
        if entry.name.endswith(".py")
    )
    digest = sha256()
    for name in sorted(names):
        digest.update(name.encode("utf-8") + b"\0" + root.joinpath(name).read_bytes())
    return {
        "agent_version": __version__,
        "harness_sha256": digest.hexdigest(),
        "python": sys.version.split()[0],
        "sdk_versions": {name: version(name) for name in ("anthropic", "openai")},
    }


@dataclass(frozen=True, slots=True)
class EvalResult:
    """One bounded evaluation outcome suitable for terminal reporting."""

    task_id: str
    passed: bool
    detail: str
    duration_ms: int = 0


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise AssertionError(detail)


def _elapsed_ms(started_at: float, finished_at: float) -> int:
    return max(round((finished_at - started_at) * 1_000), 0)


def _parse_json_lines(raw: str) -> tuple[dict[str, object], ...]:
    lines = tuple(line for line in raw.splitlines() if line.strip())
    if not lines:
        raise ValueError("missing protocol events")
    events = []
    for line in lines:
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("protocol event must be an object")
        events.append(value)
    return tuple(events)


def _parse_json_object(raw: str) -> dict[str, object]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("protocol result must be an object")
    return value


def _parse_usage(value: object) -> TokenUsage | None:
    if not isinstance(value, dict):
        return None
    names = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    counts = {name: value.get(name) for name in names}
    if any(type(count) is not int or count < 0 for count in counts.values()):
        return None
    usage = TokenUsage.model_validate(counts)
    if value.get("total_tokens") != usage.total_tokens:
        return None
    return usage
