"""Tests for fixed, workspace-scoped command tools."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from neil_agent.schemas import ToolCall
from neil_agent.tools.registry import ToolRegistry
from neil_agent.tools.shell import ShellTools


def test_quality_check_requires_preview_and_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "3 passed\n", "")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-be-inherited")
    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = ToolRegistry()
    ShellTools(tmp_path, timeout=15).register(registry)
    call = ToolCall(
        id="call-quality",
        name="run_quality_check",
        arguments={"check": "pytest"},
    )

    preview = registry.preview(call)
    denied = registry.execute(call)
    approved = registry.execute(
        call,
        approved=True,
        approved_preview=preview.content,
    )

    assert str(tmp_path) in preview.content
    assert "pytest -q" in preview.content
    assert "15 秒" in preview.content
    assert denied.is_error is True
    assert approved.is_error is False
    assert "3 passed" in approved.content
    assert len(calls) == 1
    command, options = calls[0]
    assert command == [sys.executable, "-m", "pytest", "-q"]
    assert options["cwd"] == tmp_path.resolve()
    assert options["timeout"] == 15
    assert options["shell"] is False
    assert options["stdin"] == subprocess.DEVNULL
    assert "DEEPSEEK_API_KEY" not in options["env"]
    assert "GITHUB_TOKEN" not in options["env"]


def test_quality_check_rejects_commands_outside_allowlist(tmp_path: Path) -> None:
    registry = ToolRegistry()
    ShellTools(tmp_path).register(registry)

    result = registry.preview(
        ToolCall(
            id="call-command",
            name="run_quality_check",
            arguments={"check": "python"},
        )
    )

    assert result.is_error is True
    assert "不允许的代码检查" in result.content
    assert "pytest, ruff, mypy" in result.content


def test_git_inspection_commands_do_not_require_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "clean\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = ToolRegistry()
    ShellTools(tmp_path).register(registry)

    status = registry.execute(ToolCall(id="call-status", name="git_status"))
    diff = registry.execute(
        ToolCall(
            id="call-diff",
            name="git_diff",
            arguments={"staged": True},
        )
    )

    assert status.is_error is False
    assert diff.is_error is False
    assert commands == [
        [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "status",
            "--short",
            "--branch",
            "--ignore-submodules=all",
        ],
        [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            "--cached",
        ],
    ]
    assert [definition.name for definition in registry.definitions] == [
        "run_quality_check",
        "git_status",
        "git_diff",
    ]


def test_command_timeout_becomes_tool_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def time_out(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 3, output=b"partial output")

    monkeypatch.setattr(subprocess, "run", time_out)
    registry = ToolRegistry()
    ShellTools(tmp_path, timeout=3).register(registry)

    result = registry.execute(ToolCall(id="call-status", name="git_status"))

    assert result.is_error is True
    assert "超过 3 秒" in result.content
    assert "partial output" in result.content


def test_failed_command_output_is_capped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = "BEGIN-" + ("x" * 2_000) + "-END"

    def fail(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, output, "")

    monkeypatch.setattr(subprocess, "run", fail)
    registry = ToolRegistry()
    ShellTools(tmp_path, max_output_chars=1_000).register(registry)

    result = registry.execute(ToolCall(id="call-status", name="git_status"))

    assert result.is_error is True
    assert result.content.startswith("Exit code: 1\nBEGIN-")
    assert "已省略" in result.content
    assert result.content.endswith("-END")
    assert len(result.content) < len(output)


def test_safe_environment_keeps_runtime_paths_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")

    environment = ShellTools._safe_environment()

    assert environment["PATH"] == os.environ["PATH"]
    assert "DEEPSEEK_API_KEY" not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
