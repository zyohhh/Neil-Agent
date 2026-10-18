"""Regression coverage against real Git repositories and synthetic secrets."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from neil_agent.errors import ToolError
from neil_agent.schemas import ToolCall
from neil_agent.tools.registry import ToolRegistry
from neil_agent.processes import ProcessResult
from neil_agent.tools.shell import ShellTools



def _process_result(command, returncode, stdout, stderr=""):
    return ProcessResult(returncode, stdout, stderr=stderr)

def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            *arguments,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Local regression")
    _git(tmp_path, "config", "user.email", "regression@example.invalid")
    (tmp_path / "safe.txt").write_text("old\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "safe.txt")
    _git(tmp_path, "commit", "-qm", "Initial fixture")
    return tmp_path


@pytest.mark.parametrize("path", ["目录/private.pem", ".ssh/中文.txt"])
def test_quoted_sensitive_diff_never_exposes_content(
    repository: Path, path: str
) -> None:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SYNTHETIC_SECRET_BEFORE\n", encoding="utf-8")
    _git(repository, "add", "--", path)
    _git(repository, "commit", "-qm", "Sensitive fixture")
    target.write_text("SYNTHETIC_SECRET_AFTER\n", encoding="utf-8")
    (repository / "safe.txt").write_text("visible change\n", encoding="utf-8")

    output = ShellTools(repository).git_diff()

    assert "SYNTHETIC_SECRET" not in output
    assert "visible change" in output
    assert "redacted" in output


@pytest.mark.parametrize(
    "setting", ["diff.noprefix", "diff.mnemonicPrefix", "color.ui"]
)
def test_repository_display_settings_cannot_bypass_redaction(
    repository: Path, setting: str
) -> None:
    target = repository / ".env"
    target.write_text("SYNTHETIC_SECRET_BEFORE\n", encoding="utf-8")
    _git(repository, "add", "--", ".env")
    _git(repository, "commit", "-qm", "Sensitive fixture")
    target.write_text("SYNTHETIC_SECRET_AFTER\n", encoding="utf-8")
    (repository / "safe.txt").write_text("visible change\n", encoding="utf-8")
    _git(repository, "config", setting, "always" if setting == "color.ui" else "true")

    output = ShellTools(repository).git_diff()

    assert "SYNTHETIC_SECRET" not in output
    assert "visible change" in output
    assert "\x1b" not in output


@pytest.mark.parametrize(
    "inspection",
    [
        "git_diff",
        "git_status",
        "git_status_snapshot",
        "git_review_status_snapshot",
        "git_numstat_snapshot",
        "git_file_diff_snapshot",
        "preview_git_stage",
    ],
)
def test_readonly_git_does_not_execute_configured_filters(
    repository: Path, inspection: str
) -> None:
    probe = repository / "filter_probe.py"
    probe.write_text(
        "from pathlib import Path\nimport sys\n"
        "Path('filter-executed').write_text('called')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    (repository / ".gitattributes").write_text(
        "safe.txt filter=probe\n", encoding="utf-8"
    )
    _git(
        repository,
        "config",
        "filter.probe.clean",
        f'"{Path(sys.executable).as_posix()}" "{probe.as_posix()}"',
    )
    _git(repository, "config", "filter.probe.required", "true")
    (repository / "safe.txt").write_text("changed content\n", encoding="utf-8")

    tools = ShellTools(repository)
    if inspection in {"git_file_diff_snapshot", "preview_git_stage"}:
        output = getattr(tools, inspection)(["safe.txt"])
    else:
        output = getattr(tools, inspection)()

    assert output
    assert not (repository / "filter-executed").exists()


def test_readonly_git_does_not_start_process_filter(repository: Path) -> None:
    probe = repository / "process_probe.py"
    probe.write_text(
        "from pathlib import Path\nPath('process-executed').write_text('called')\n",
        encoding="utf-8",
    )
    (repository / ".gitattributes").write_text(
        "safe.txt filter=probe\n", encoding="utf-8"
    )
    _git(
        repository,
        "config",
        "filter.probe.process",
        f'"{Path(sys.executable).as_posix()}" "{probe.as_posix()}"',
    )
    _git(repository, "config", "filter.probe.required", "true")
    (repository / "safe.txt").write_text("changed content\n", encoding="utf-8")

    assert "changed content" in ShellTools(repository).git_diff()
    assert not (repository / "process-executed").exists()


@pytest.mark.parametrize(
    "operation",
    ["git_status", "git_diff", "git_review_status_snapshot", "git_numstat_snapshot"],
)
def test_git_cannot_discover_a_repository_above_workspace(
    repository: Path, operation: str
) -> None:
    workspace = repository / "selected-workspace"
    workspace.mkdir()
    (repository / "safe.txt").write_text(
        "SYNTHETIC_OUTSIDE_WORKSPACE\n", encoding="utf-8"
    )

    with pytest.raises(ToolError):
        getattr(ShellTools(workspace), operation)()


def test_commit_approval_binds_redacted_content(repository: Path) -> None:
    target = repository / ".env"
    target.write_text("SYNTHETIC_FIRST\n", encoding="utf-8")
    _git(repository, "add", "--", ".env")
    registry = ToolRegistry()
    ShellTools(repository).register(registry)
    call = ToolCall(
        id="commit-binding", name="git_commit", arguments={"message": "Fixture change"}
    )
    preview = registry.preview(call)
    assert not preview.is_error
    assert "SYNTHETIC_FIRST" not in preview.content
    before = _git(repository, "rev-parse", "HEAD")
    target.write_text("SYNTHETIC_OTHER\n", encoding="utf-8")
    _git(repository, "add", "--", ".env")

    result = registry.execute(call, approved=True, approved_preview=preview.content)

    assert result.is_error
    assert "确认后发生变化" in result.content
    assert _git(repository, "rev-parse", "HEAD") == before


def test_repository_config_cannot_redirect_the_worktree(
    repository: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("other-workspace")
    (outside / "safe.txt").write_text("SYNTHETIC_OUTSIDE_CONTENT\n", encoding="utf-8")
    _git(repository, "config", "core.worktree", str(outside))
    (repository / "safe.txt").write_text("inside-workspace-change\n", encoding="utf-8")

    output = ShellTools(repository).git_diff()

    assert "SYNTHETIC_OUTSIDE_CONTENT" not in output
    assert "inside-workspace-change" in output


def test_colored_status_cannot_bypass_sensitive_path_filter(repository: Path) -> None:
    (repository / ".env").write_text("SYNTHETIC_SECRET\n", encoding="utf-8")
    _git(repository, "config", "color.status", "always")

    output = ShellTools(repository).git_status()

    assert ".env" not in output
    assert "redacted sensitive path" in output
    assert "\x1b" not in output


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_failed_git_diff_does_not_leak_partial_sensitive_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    raw = 'diff --git "a/.ssh/private key" "b/.ssh/private key"\n+SYNTHETIC_SECRET\n'

    def failed(
        command: list[str], **_kwargs: object
    ) -> ProcessResult:
        if failure == "timeout":
            return ProcessResult(-1, raw, "timeout")
        return _process_result(command, 1, raw, "")

    monkeypatch.setattr("neil_agent.tools.shell.run_bounded", failed)
    with pytest.raises(ToolError) as error:
        ShellTools(tmp_path).git_diff()
    assert "SYNTHETIC_SECRET" not in str(error.value)
    assert "redacted" in str(error.value)
