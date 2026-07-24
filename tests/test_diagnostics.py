"""Tests for read-only, secret-safe local diagnostics."""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from neil_agent.audit import AUDIT_LOCK_FILENAME, JsonlAuditSink, _AuditFileLock
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
        "生命周期审计",
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


def test_doctor_inspects_enabled_audit_without_exposing_records(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, audit_log_enabled=True)
    sink = JsonlAuditSink(tmp_path, max_bytes=settings.audit_log_max_bytes)
    sink.validate()
    shell_tools = MagicMock(spec=ShellTools)
    shell_tools.git_status_snapshot.return_value = "## main"

    report = run_diagnostics(
        settings,
        tmp_path,
        SessionStore(tmp_path),
        cast(ShellTools, shell_tools),
    )

    audit_check = next(check for check in report.checks if check.name == "生命周期审计")
    assert audit_check.status == "ok"
    assert audit_check.summary == "元数据审计可用"
    assert "top-secret-key" not in repr(audit_check)
    assert any("跨进程锁：可用" in detail for detail in audit_check.details)


def test_doctor_warns_about_invalid_audit_json_without_echoing_it(
    tmp_path: Path,
) -> None:
    secret = "PRIVATE-AUDIT-CONTENT"
    settings = _settings(tmp_path, audit_log_enabled=True)
    sink = JsonlAuditSink(tmp_path, max_bytes=settings.audit_log_max_bytes)
    sink.validate()
    sink.path.write_text(secret, encoding="utf-8")
    shell_tools = MagicMock(spec=ShellTools)
    shell_tools.git_status_snapshot.return_value = "## main"

    report = run_diagnostics(
        settings,
        tmp_path,
        SessionStore(tmp_path),
        cast(ShellTools, shell_tools),
    )

    audit_check = next(check for check in report.checks if check.name == "生命周期审计")
    assert audit_check.status == "warning"
    assert "无效审计记录" in audit_check.summary
    assert secret not in repr(audit_check)


def test_doctor_does_not_read_audit_records_while_lock_is_busy(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, audit_log_enabled=True)
    sink = JsonlAuditSink(tmp_path, max_bytes=settings.audit_log_max_bytes)
    sink.validate()
    lock_path = tmp_path / ".neil-agent" / "audit" / AUDIT_LOCK_FILENAME
    holder = _AuditFileLock(lock_path, timeout=1, create=False)
    assert holder.acquire() is True
    shell_tools = MagicMock(spec=ShellTools)
    shell_tools.git_status_snapshot.return_value = "## main"

    try:
        report = run_diagnostics(
            settings,
            tmp_path,
            SessionStore(tmp_path),
            cast(ShellTools, shell_tools),
        )
    finally:
        holder.close()

    audit_check = next(check for check in report.checks if check.name == "生命周期审计")
    assert audit_check.status == "warning"
    assert "另一进程占用" in audit_check.summary
    assert any("锁占用期间未读取" in detail for detail in audit_check.details)
