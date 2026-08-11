"""Read-only local diagnostics for the interactive ``/doctor`` command."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .audit import JsonlAuditSink
from .config import Settings
from .errors import AuditError, NeilAgentError, SandboxError, SessionError
from .sandbox import SandboxCapabilities, WindowsSandboxBackend
from .session import SessionStore
from .tools.shell import ShellTools

DiagnosticStatus = Literal["ok", "warning", "error"]
_SANDBOX_REASON_SUMMARIES = {
    "unsupported_platform": "当前平台不支持所选后端",
    "executable_not_found": "未找到 Windows Sandbox 可执行文件",
    "cli_executable_required": "缺少可认证的 Windows Sandbox CLI",
    "certification_required": "缺少匹配当前构建的安全认证证据",
    "certification_invalid": "认证证据未通过完整重放或当前宿主绑定",
    "execution_channel_unavailable": "受控执行与结果回传通道尚未就绪",
    "ready": "全部强制安全门禁已就绪",
}


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
            _check_sandbox(settings),
            _check_sessions(session_store),
            _check_audit(settings, workspace_root),
            _check_git(shell_tools),
        )
    )


def _check_configuration(settings: Settings) -> DiagnosticCheck:
    endpoint = settings.selected_base_url
    secure_endpoint = endpoint is None or endpoint.scheme == "https"
    key_status = (
        "已配置（值已隐藏）" if settings.selected_api_key is not None else "无需配置"
    )
    return DiagnosticCheck(
        name="配置",
        status="ok" if secure_endpoint else "warning",
        summary="配置已通过校验" if secure_endpoint else "API 地址未使用 HTTPS",
        details=(
            f"Provider：{settings.llm_provider.value}",
            f"API Key：{key_status}",
            f"模型：{settings.selected_model}",
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


def _check_sandbox(settings: Settings) -> DiagnosticCheck:
    if settings.sandbox_backend == "disabled":
        return DiagnosticCheck(
            name="OS 沙箱",
            status="ok",
            summary="未启用（可选）",
            details=("SANDBOX_BACKEND=disabled",),
        )

    try:
        capabilities = WindowsSandboxBackend(
            certification_root=settings.sandbox_certification_root,
            trusted_reviewer=settings.sandbox_trusted_reviewer,
            trusted_review_sha256=settings.sandbox_trusted_review_sha256,
        ).probe()
    except (SandboxError, OSError, ValueError):
        return DiagnosticCheck(
            name="OS 沙箱",
            status="error",
            summary="Windows Sandbox 能力探测失败",
            details=("探测过程已 fail-closed，未启动任何命令。",),
        )

    complete = _sandbox_capabilities_complete(
        capabilities,
        expected_backend=settings.sandbox_backend,
    )
    if complete:
        summary = "Windows Sandbox 已就绪"
    elif capabilities.available:
        summary = "Windows Sandbox 能力不完整"
    else:
        summary = "Windows Sandbox 不可用"

    workspace_modes = "、".join(capabilities.workspace_modes) or "无"
    network_modes = "、".join(capabilities.network_modes) or "无"
    reason_code = (
        capabilities.reason_code
        if capabilities.reason_code in _SANDBOX_REASON_SUMMARIES
        else "unknown"
    )
    return DiagnosticCheck(
        name="OS 沙箱",
        status="ok" if complete else "error",
        summary=summary,
        details=(
            f"后端：{settings.sandbox_backend}",
            f"探测结果：{_SANDBOX_REASON_SUMMARIES.get(reason_code, '未知状态')}",
            f"原因代码：{reason_code}",
            "认证证据："
            f"{'已验证并绑定' if capabilities.certification is not None else '缺失'}",
            f"工作区模式：{workspace_modes}",
            f"网络模式：{network_modes}",
            "安全门禁："
            f"取消={_support_label(capabilities.supports_cancellation)}，"
            f"超时={_support_label(capabilities.supports_timeout)}，"
            f"输出限制={_support_label(capabilities.supports_output_limit)}，"
            f"内存限制={_support_label(capabilities.supports_memory_limit)}，"
            f"进程限制={_support_label(capabilities.supports_process_limit)}",
        ),
    )


def _sandbox_capabilities_complete(
    capabilities: SandboxCapabilities,
    *,
    expected_backend: str,
) -> bool:
    return capabilities.backend == expected_backend and capabilities.ready


def _support_label(supported: bool) -> str:
    return "支持" if supported else "缺失"


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


def _check_audit(settings: Settings, workspace_root: Path) -> DiagnosticCheck:
    if not settings.audit_log_enabled:
        return DiagnosticCheck(
            name="生命周期审计",
            status="ok",
            summary="未启用（可选）",
            details=("AUDIT_LOG_ENABLED=false",),
        )
    try:
        status = JsonlAuditSink(
            workspace_root,
            max_bytes=settings.audit_log_max_bytes,
        ).inspect()
    except AuditError:
        return DiagnosticCheck(
            name="生命周期审计",
            status="error",
            summary="审计日志不可用",
            details=("请检查 .neil-agent/audit 的路径、锁文件和普通文件边界。",),
        )

    oversized = (
        status.current_size_bytes > status.max_bytes
        or status.backup_size_bytes > status.max_bytes
    )
    invalid_records = status.invalid_records or 0
    if not status.lock_available:
        diagnostic_status: DiagnosticStatus = "warning"
        summary = "审计日志当前由另一进程占用"
    elif invalid_records:
        diagnostic_status = "warning"
        summary = f"发现 {invalid_records} 条无效审计记录"
    elif oversized:
        diagnostic_status = "warning"
        summary = "审计日志超过配置的轮转上限"
    else:
        diagnostic_status = "ok"
        summary = "元数据审计可用"

    if status.current_records is None:
        record_detail = "记录结构：锁占用期间未读取"
    else:
        record_detail = (
            f"记录：当前 {status.current_records} 条，"
            f"备份 {status.backup_records or 0} 条"
        )
    return DiagnosticCheck(
        name="生命周期审计",
        status=diagnostic_status,
        summary=summary,
        details=(
            f"日志：{status.path}",
            f"当前大小：{_format_bytes(status.current_size_bytes)}",
            f"备份大小：{_format_bytes(status.backup_size_bytes)}",
            f"轮转上限：{_format_bytes(status.max_bytes)}",
            record_detail,
            f"跨进程锁：{'可用' if status.lock_available else '占用中'}",
        ),
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
