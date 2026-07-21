"""Tests for read-only, secret-safe local diagnostics."""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from neil_agent.config import Settings
from neil_agent.diagnostics import run_diagnostics
from neil_agent.errors import ToolError
from neil_agent.session import SessionStore
from neil_agent.tools.shell import ShellTools


def _settings(tmp_path: Path, **updates: object) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="top-secret-key",
        workspace_root=tmp_path,
        **updates,
    )


def test_doctor_reports_healthy_local_state_without_revealing_api_key(
    tmp_path: Path,
) -> None:
    shell_tools = MagicMock(spec=ShellTools)
    shell_tools.git_status_snapshot.return_value = "## main...origin/main"

    report = run_diagnostics(
        _settings(tmp_path),
        tmp_path,
        SessionStore(tmp_path),
        cast(ShellTools, shell_tools),
    )

    assert [check.name for check in report.checks] == [
        "配置",
        "工作区",
        "本地会话",
        "Git",
    ]
    assert all(check.status == "ok" for check in report.checks)
    assert report.error_count == 0
    assert report.warning_count == 0
    assert "top-secret-key" not in repr(report)


def test_doctor_warns_for_insecure_endpoint_corrupt_session_and_missing_git(
    tmp_path: Path,
) -> None:
    session_store = SessionStore(tmp_path)
    session_store.root.mkdir(parents=True)
    (session_store.root / "broken.json").write_text("not json", encoding="utf-8")
    shell_tools = MagicMock(spec=ShellTools)
    shell_tools.git_status_snapshot.side_effect = ToolError("git is unavailable")

    report = run_diagnostics(
        _settings(tmp_path, deepseek_base_url="http://localhost:9000"),
        tmp_path,
        session_store,
        cast(ShellTools, shell_tools),
    )

    statuses = {check.name: check.status for check in report.checks}
    assert statuses["配置"] == "warning"
    assert statuses["本地会话"] == "warning"
    assert statuses["Git"] == "warning"
    assert report.warning_count == 3
    assert "git is unavailable" not in repr(report)
