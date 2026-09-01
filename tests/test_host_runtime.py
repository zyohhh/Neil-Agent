"""Tests for shared host runtime assembly."""

from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest

from neil_agent.config import Settings
from neil_agent.host_runtime import (
    BENCHMARK_MINIMAL_READONLY_TOOLS,
    BENCHMARK_MINIMAL_WRITE_TOOLS,
    HostMode,
    RuntimeProfile,
    build_host_runtime,
    instruction_target,
    observe_host_security,
    resolve_runtime_profile,
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


def test_instruction_target_uses_cwd_inside_workspace(
    tmp_path: Path, monkeypatch
) -> None:
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
    assert runtime.profile.runtime_profile is RuntimeProfile.STANDARD
    assert "write_file" in runtime.profile.tool_names
    assert "set_task_plan" in runtime.profile.tool_names
    assert "load_skill" in runtime.profile.tool_names
    assert runtime.profile.instruction_scope == "cwd"
    assert runtime.profile.task_tools_enabled is True
    assert runtime.profile.sandbox_tools_enabled is False
    assert runtime.task_tracker is not None
    assert runtime.shell is not None


def test_build_host_runtime_noninteractive_readonly_is_read_only(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    runtime = build_host_runtime(settings, mode=HostMode.NONINTERACTIVE_READONLY)
    assert "write_file" not in runtime.profile.tool_names
    assert "git_commit" not in runtime.profile.tool_names
    assert "read_file" in runtime.profile.tool_names
    assert "load_skill" not in runtime.profile.tool_names
    assert runtime.profile.task_tools_enabled is False
    assert runtime.task_tracker is None


def test_build_host_runtime_web_defaults_to_web_safe_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    nested = tmp_path / "pkg"
    nested.mkdir()
    monkeypatch.chdir(nested)
    settings = _settings(tmp_path)
    runtime = build_host_runtime(settings, mode=HostMode.WEB)
    assert runtime.profile.runtime_profile is RuntimeProfile.WEB_SAFE


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


def test_cli_and_web_share_security_projection_semantics(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    cli = build_host_runtime(settings, mode=HostMode.CLI)
    web = build_host_runtime(settings, mode=HostMode.WEB)

    cli_security = observe_host_security(settings, cli.registry)
    web_security = observe_host_security(settings, web.registry)

    assert web_security.application == cli_security.application
    assert web_security.os_sandbox == cli_security.os_sandbox
    assert web_security.audit_status == cli_security.audit_status
    assert web_security.tool_count == cli_security.tool_count
    assert web_security.approval_tool_count == cli_security.approval_tool_count


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


def test_benchmark_minimal_profile_exposes_only_bounded_file_tools(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    readonly = build_host_runtime(
        settings,
        mode=HostMode.NONINTERACTIVE_READONLY,
        profile=RuntimeProfile.BENCHMARK_MINIMAL,
    )
    assert readonly.profile.runtime_profile is RuntimeProfile.BENCHMARK_MINIMAL
    assert readonly.profile.tool_names == BENCHMARK_MINIMAL_READONLY_TOOLS
    assert "git_status" not in readonly.profile.tool_names
    assert "run_quality_check" not in readonly.profile.tool_names

    write_mode = build_host_runtime(
        settings,
        mode=HostMode.NONINTERACTIVE_WRITE,
        profile=RuntimeProfile.BENCHMARK_MINIMAL,
    )
    assert write_mode.profile.tool_names == BENCHMARK_MINIMAL_WRITE_TOOLS
    assert "write_file" not in write_mode.profile.tool_names
    assert "import_guest_export" not in write_mode.profile.tool_names
    assert "set_task_plan" not in write_mode.profile.tool_names
    assert write_mode.guest_import is None


def test_resolve_runtime_profile_uses_web_safe_for_web_mode() -> None:
    assert resolve_runtime_profile(HostMode.WEB) is RuntimeProfile.WEB_SAFE
    assert resolve_runtime_profile(HostMode.CLI) is RuntimeProfile.STANDARD


def test_host_runtime_close_unregisters_tools_and_hooks(tmp_path: Path) -> None:
    settings = _settings(tmp_path, audit_log_enabled=True)
    runtime = build_host_runtime(settings, mode=HostMode.CLI)
    assert runtime.registry.definitions
    assert any(runtime.hooks._callbacks[stage] for stage in runtime.hooks._callbacks)

    runtime.close()

    assert runtime.closed
    assert runtime.registry.definitions == ()
    assert all(not callbacks for callbacks in runtime.hooks._callbacks.values())
    runtime.close()


def test_host_runtime_close_runs_disposers_in_reverse_order(tmp_path: Path) -> None:
    runtime = build_host_runtime(_settings(tmp_path), mode=HostMode.CLI)
    order: list[str] = []
    runtime._disposers.clear()
    runtime._disposers.extend(
        [
            lambda: order.append("first"),
            lambda: order.append("second"),
        ]
    )

    runtime.close()

    assert order == ["second", "first"]


def test_workbench_service_close_marks_host_runtime_closed(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from neil_agent.web.service import WorkbenchSnapshotService

    service = WorkbenchSnapshotService(
        _settings(tmp_path),
        clock=lambda: datetime.now(timezone.utc),
    )
    assert not service.host_runtime_closed
    assert service.registry.definitions

    service.close()

    assert service.host_runtime_closed
    assert service.registry.definitions == ()


class _EchoModel:
    def complete(self, messages, *, system_prompt):  # type: ignore[no-untyped-def]
        return "ok"

    def stream(self, messages, *, system_prompt, tools=()):  # type: ignore[no-untyped-def]
        yield "ok"
        from neil_agent.schemas import ModelResponse

        yield ModelResponse(content="ok")


def test_build_agent_binds_runtime_registry_and_instructions(tmp_path: Path) -> None:
    from neil_agent.host_runtime import build_agent

    (tmp_path / "AGENTS.md").write_text("BOUND-INSTRUCTION", encoding="utf-8")
    settings = _settings(tmp_path)
    runtime = build_host_runtime(settings, mode=HostMode.CLI)
    try:
        agent = build_agent(settings, runtime, _EchoModel())
        assert agent._registry is runtime.registry
        assert "BOUND-INSTRUCTION" in agent._system_prompt
        assert agent._task_tracker is runtime.task_tracker
        assert "".join(agent.stream_chat("hello")) == "ok"
    finally:
        runtime.close()


def test_agent_turn_worker_reuses_injected_host_runtime(tmp_path: Path) -> None:
    from unittest.mock import patch

    from neil_agent.host_runtime import build_host_runtime
    from neil_agent.web.controller import AgentTurnWorker

    settings = _settings(tmp_path)
    runtime = build_host_runtime(settings, mode=HostMode.WEB)
    worker = AgentTurnWorker(settings, host_runtime=runtime)
    try:
        with patch(
            "neil_agent.web.controller.create_provider",
            return_value=_EchoModel(),
        ):
            first = worker.run(
                "one",
                None,
                Event(),
                lambda text: None,
                lambda event: None,
                lambda event: None,
                lambda call, preview: True,
            )
            second = worker.run(
                "two",
                None,
                Event(),
                lambda text: None,
                lambda event: None,
                lambda event: None,
                lambda call, preview: True,
            )
        assert first.messages[-1].content == "ok"
        assert second.messages[-1].content == "ok"
        assert not runtime.closed
        assert runtime.registry.definitions
    finally:
        runtime.close()

