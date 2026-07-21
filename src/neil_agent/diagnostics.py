"""Read-only local diagnostics for the interactive ``/doctor`` command."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Settings
from .errors import NeilAgentError, SessionError
from .session import SessionStore
from .tools.shell import ShellTools

DiagnosticStatus = Literal["ok", "warning", "error"]


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    """One local check with safe, user-visible details."""

    name: str
    status: DiagnosticStatus
    summary: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    """A complete set of local diagnostics."""

    checks: tuple[DiagnosticCheck, ...]

    @property
    def warning_count(self) -> int:
        return sum(check.status == "warning" for check in self.checks)

    @property
    def error_count(self) -> int:
        return sum(check.status == "error" for check in self.checks)


def run_diagnostics(
    settings: Settings,
    workspace_root: Path,
    session_store: SessionStore,
    shell_tools: ShellTools,
) -> DiagnosticReport:
    """Inspect local state without sending a model request or revealing secrets."""

    return DiagnosticReport(
        checks=(
            _check_configuration(settings),
            _check_workspace(workspace_root),
            _check_sessions(session_store),
            _check_git(shell_tools),
        )
    )


def _check_configuration(settings: Settings) -> DiagnosticCheck:
    secure_endpoint = settings.deepseek_base_url.scheme == "https"
    return DiagnosticCheck(
        name="配置",
        status="ok" if secure_endpoint else "warning",
        summary="配置已通过校验" if secure_endpoint else "API 地址未使用 HTTPS",
        details=(
            "API Key：已配置（值已隐藏）",
            f"模型：{settings.deepseek_model}",
            f"请求超时：{settings.request_timeout:g} 秒",
            f"失败重试：最多 {settings.max_retries} 次，"
            f"等待上限 {settings.retry_max_delay:g} 秒",
        ),
    )


def _check_workspace(workspace_root: Path) -> DiagnosticCheck:
    readable = os.access(workspace_root, os.R_OK)
    writable = os.access(workspace_root, os.W_OK)
    if readable and writable:
        status: DiagnosticStatus = "ok"
        summary = "工作区可读写"
    elif readable:
        status = "warning"
        summary = "工作区只读，修改工具将不可用"
    else:
        status = "error"
        summary = "工作区不可读"
    return DiagnosticCheck(
        name="工作区",
        status=status,
        summary=summary,
        details=(f"路径：{workspace_root}",),
    )


def _check_sessions(session_store: SessionStore) -> DiagnosticCheck:
    try:
        index = session_store.list_sessions()
    except SessionError:
        return DiagnosticCheck(
            name="本地会话",
            status="error",
            summary="会话目录不可用",
            details=("请检查 .neil-agent 目录权限及是否存在符号链接。",),
        )
    if index.invalid_count:
        status: DiagnosticStatus = "warning"
        summary = f"发现 {index.invalid_count} 个损坏或不兼容文件"
    else:
        status = "ok"
        summary = "会话存储可用"
    return DiagnosticCheck(
        name="本地会话",
        status=status,
        summary=summary,
        details=(
            f"有效会话：{index.valid_count} 个",
            f"JSON 文件占用：{_format_bytes(index.total_size_bytes)}",
        ),
    )


def _check_git(shell_tools: ShellTools) -> DiagnosticCheck:
    try:
        snapshot = shell_tools.git_status_snapshot()
    except NeilAgentError:
        return DiagnosticCheck(
            name="Git",
            status="warning",
            summary="Git 状态不可用",
            details=("请确认 Git 已安装，且工作区是 Git 仓库。",),
        )
    lines = snapshot.splitlines()
    dirty = len(lines) > 1 or bool(lines and not lines[0].startswith("##"))
    return DiagnosticCheck(
        name="Git",
        status="ok",
        summary="Git 仓库可访问",
        details=(f"工作区状态：{'有未提交变更' if dirty else '干净'}",),
    )


def _format_bytes(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")
