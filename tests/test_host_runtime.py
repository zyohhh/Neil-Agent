"""Tests for shared host runtime assembly."""

from __future__ import annotations

from pathlib import Path

import pytest

from neil_agent.config import Settings
from neil_agent.host_runtime import (
    HostMode,
    build_host_runtime,
    instruction_target,
    observe_host_security,
    windows_sandbox_backend,
)


def _settings(root: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "deepseek_api_key": "test-key",
        "workspace_root": root,
        "llm_model": "deepseek-test-model",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_instruction_target_uses_cwd_inside_workspace(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "pkg"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert instruction_target(workspace.resolve()) == nested.resolve()


def test_instruction_target_falls_back_to_workspace_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert instruction_target(workspace.resolve()) == workspace.resolve()


def test_build_host_runtime_cli_registers_write_and_task_tools(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path)
    runtime = build_host_runtime(settings, mode=HostMode.CLI)
    assert "write_file" in runtime.profile.tool_names
    assert "set_task_plan" in runtime.profile.tool_names
    assert runtime.profile.instruction_scope == "cwd"
    assert runtime.profile.task_tools_enabled is True
    assert runtime.profile.sandbox_tools_enabled is False
    assert runtime.task_tracker is not None
    assert runtime.shell is not None


def test_build_host_runtime_noninteractive_readonly_is_read_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = build_host_runtime(settings, mode=HostMode.NONINTERACTIVE_READONLY)
    assert "write_file" not in runtime.profile.tool_names
    assert "git_commit" not in runtime.profile.tool_names
    assert "read_file" in runtime.profile.tool_names
    assert runtime.profile.task_tools_enabled is False
    assert runtime.task_tracker is None


def test_build_host_runtime_web_matches_cli_instruction_and_sandbox_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    nested = tmp_path / "pkg"
    nested.mkdir()
    monkeypatch.chdir(nested)
    settings = _settings(tmp_path)
    runtime = build_host_runtime(settings, mode=HostMode.WEB)
    assert "write_file" in runtime.profile.tool_names
    assert runtime.profile.instruction_scope == "cwd"
    assert runtime.instruction_manager.current.target == nested.resolve()
    assert runtime.profile.sandbox_tools_enabled is False
    assert runtime.profile.task_tools_enabled is True


def test_build_host_runtime_web_registers_sandbox_when_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "neil_agent.host_runtime._register_sandbox_tools",
        lambda *_args, **_kwargs: True,
    )
    settings = _settings(
        tmp_path,
        sandbox_backend="windows-sandbox",
        sandbox_certification_root=str(tmp_path / "cert"),
        sandbox_trusted_reviewer="reviewer@example.com",
        sandbox_trusted_review_sha256="a" * 64,
    )
    runtime = build_host_runtime(settings, mode=HostMode.WEB)
    assert runtime.profile.sandbox_tools_enabled is True


def test_windows_sandbox_backend_uses_settings(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        sandbox_backend="windows-sandbox",
        sandbox_certification_root=str(tmp_path / "cert"),
        sandbox_trusted_reviewer="reviewer@example.com",
        sandbox_trusted_review_sha256="a" * 64,
    )
    backend = windows_sandbox_backend(settings)
    assert backend._runtime_certification_root == (tmp_path / "cert").resolve()


def test_observe_host_security_uses_registry_permissions(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = build_host_runtime(settings, mode=HostMode.WEB)
    shield = observe_host_security(settings, runtime.registry)

    assert shield.tool_count == len(runtime.profile.tool_names)
    assert shield.direct_tool_count + shield.approval_tool_count == shield.tool_count
    assert shield.audit_enabled is False
    assert shield.audit_status == "disabled"


@pytest.mark.parametrize(
    ("mode", "expected_write_tools"),
    [
        (HostMode.CLI, True),
        (HostMode.NONINTERACTIVE_WRITE, True),
        (HostMode.NONINTERACTIVE_READONLY, False),
        (HostMode.WEB, True),
    ],
)
def test_host_mode_write_tool_matrix(
    tmp_path: Path,
    mode: HostMode,
    expected_write_tools: bool,
) -> None:
    settings = _settings(tmp_path)
    runtime = build_host_runtime(settings, mode=mode)
    has_write = "write_file" in runtime.profile.tool_names
    assert has_write is expected_write_tools
