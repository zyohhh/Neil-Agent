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
    assert "Command:" in approved.content
    assert "Working directory:" in approved.content
    assert "Exit code: 0" in approved.content
    assert "Output:" in approved.content
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
        "git_stage",
        "git_commit",
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
    assert result.content.startswith("Command: git")
    assert "Exit code: 1\nOutput:\nBEGIN-" in result.content
    assert "已省略" in result.content
    assert result.content.endswith("-END")
    assert len(result.content) < len(output)


def test_git_stage_previews_and_stages_only_explicit_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('new')\n", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "status" in command:
            return subprocess.CompletedProcess(command, 0, " M src/app.py\n", "")
        if "ls-files" in command:
            return subprocess.CompletedProcess(command, 0, "src/app.py\n", "")
        if "diff" in command and "--stat" in command:
            return subprocess.CompletedProcess(command, 0, "src/app.py | 1 +\n", "")
        if "diff" in command and "--cached" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "diff" in command:
            diff = "--- a/src/app.py\n+++ b/src/app.py\n+print('new')\n"
            return subprocess.CompletedProcess(command, 0, diff, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = ToolRegistry()
    ShellTools(tmp_path).register(registry)
    call = ToolCall(
        id="call-stage",
        name="git_stage",
        arguments={"paths": ["src/app.py"]},
    )

    preview = registry.preview(call)
    denied = registry.execute(call)
    approved = registry.execute(
        call,
        approved=True,
        approved_preview=preview.content,
    )

    assert preview.is_error is False
    assert "不会暂存整个工作区" in preview.content
    assert "clean filter" in preview.content
    assert "src/app.py" in preview.content
    assert "Change-ID:" in preview.content
    assert denied.is_error is True
    assert approved.is_error is False
    add_commands = [command for command in commands if "add" in command]
    assert add_commands == [
        [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "add",
            "--",
            ":(literal)src/app.py",
        ]
    ]


def test_git_stage_previews_untracked_text_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "new.py").write_text("value = 1\n", encoding="utf-8")

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "status" in command:
            return subprocess.CompletedProcess(command, 0, "?? new.py\n", "")
        if "ls-files" in command:
            return subprocess.CompletedProcess(command, 1, "", "not tracked")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = ToolRegistry()
    ShellTools(tmp_path).register(registry)

    preview = registry.preview(
        ToolCall(
            id="call-stage",
            name="git_stage",
            arguments={"paths": ["new.py"]},
        )
    )

    assert preview.is_error is False
    assert "--- /dev/null" in preview.content
    assert "+++ b/new.py" in preview.content
    assert "+value = 1" in preview.content


@pytest.mark.parametrize("path", [".", ".env", "../outside.py", "private.pem"])
def test_git_stage_rejects_broad_sensitive_or_outside_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path: str,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Git must not run for rejected paths"),
    )
    registry = ToolRegistry()
    ShellTools(tmp_path).register(registry)

    preview = registry.preview(
        ToolCall(
            id="call-stage",
            name="git_stage",
            arguments={"paths": [path]},
        )
    )

    assert preview.is_error is True


def test_git_stage_rejects_stale_preview_when_file_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "app.py"
    target.write_text("first\n", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "status" in command:
            return subprocess.CompletedProcess(command, 0, " M app.py\n", "")
        if "ls-files" in command:
            return subprocess.CompletedProcess(command, 0, "app.py\n", "")
        if "diff" in command:
            return subprocess.CompletedProcess(command, 0, "fixed diff\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = ToolRegistry()
    ShellTools(tmp_path).register(registry)
    call = ToolCall(
        id="call-stage",
        name="git_stage",
        arguments={"paths": ["app.py"]},
    )
    preview = registry.preview(call)
    target.write_text("second\n", encoding="utf-8")

    result = registry.execute(
        call,
        approved=True,
        approved_preview=preview.content,
    )

    assert result.is_error is True
    assert "确认后发生变化" in result.content
    assert not any("add" in command for command in commands)


def test_git_commit_previews_staged_diff_and_creates_only_local_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "diff" in command and "--stat" in command:
            return subprocess.CompletedProcess(command, 0, "app.py | 1 +\n", "")
        if "diff" in command:
            diff = "--- a/app.py\n+++ b/app.py\n+print('new')\n"
            return subprocess.CompletedProcess(command, 0, diff, "")
        if "commit" in command:
            return subprocess.CompletedProcess(
                command, 0, "[main abc1234] Update app\n", ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = ToolRegistry()
    ShellTools(tmp_path).register(registry)
    call = ToolCall(
        id="call-commit",
        name="git_commit",
        arguments={"message": "Update app"},
    )

    preview = registry.preview(call)
    denied = registry.execute(call)
    approved = registry.execute(
        call,
        approved=True,
        approved_preview=preview.content,
    )

    assert "不会推送到远端" in preview.content
    assert "提交消息：Update app" in preview.content
    assert "Change-ID:" in preview.content
    assert denied.is_error is True
    assert approved.is_error is False
    commit_commands = [command for command in commands if "commit" in command]
    assert commit_commands == [
        [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            "Update app",
        ]
    ]


def test_git_commit_rejects_empty_staging_area(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    registry = ToolRegistry()
    ShellTools(tmp_path).register(registry)

    result = registry.preview(
        ToolCall(
            id="call-commit",
            name="git_commit",
            arguments={"message": "Empty commit"},
        )
    )

    assert result.is_error is True
    assert "暂存区为空" in result.content


def test_git_commit_rejects_stale_truncated_diff_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    middle = ["first"]
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "diff" in command and "--stat" in command:
            return subprocess.CompletedProcess(command, 0, "app.py | 1 +\n", "")
        if "diff" in command:
            diff = ("a" * 1_500) + middle[0] + ("z" * 1_500)
            return subprocess.CompletedProcess(command, 0, diff, "")
        return subprocess.CompletedProcess(command, 0, "committed", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = ToolRegistry()
    ShellTools(tmp_path, max_output_chars=1_000).register(registry)
    call = ToolCall(
        id="call-commit",
        name="git_commit",
        arguments={"message": "Update app"},
    )
    preview = registry.preview(call)
    middle[0] = "second"

    result = registry.execute(
        call,
        approved=True,
        approved_preview=preview.content,
    )

    assert result.is_error is True
    assert "确认后发生变化" in result.content
    assert not any("commit" in command for command in commands)


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
