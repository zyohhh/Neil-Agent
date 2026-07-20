"""Tests for filesystem workspace and sensitive-file boundaries."""

from pathlib import Path

import pytest

from neil_agent.errors import ToolError
from neil_agent.schemas import ToolCall
from neil_agent.tools import filesystem as filesystem_module
from neil_agent.tools.filesystem import FileSystemTools
from neil_agent.tools.registry import ToolRegistry


def test_rejects_path_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    tools = FileSystemTools(workspace)

    with pytest.raises(ToolError, match="工作区之外"):
        tools.read_file("../secret.txt")


def test_hides_env_and_private_key_files(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("API_KEY=secret", encoding="utf-8")
    (tmp_path / ".env.example").write_text("API_KEY=example", encoding="utf-8")
    (tmp_path / "private.pem").write_text("secret", encoding="utf-8")
    tools = FileSystemTools(tmp_path)

    listing = tools.list_directory()

    assert ".env.example" in listing
    assert ".env (" not in listing
    assert "private.pem" not in listing
    with pytest.raises(ToolError, match="敏感文件"):
        tools.read_file(".env")
    with pytest.raises(ToolError, match="敏感文件"):
        tools.read_file("private.pem")


def test_search_skips_blocked_directories(tmp_path: Path) -> None:
    (tmp_path / "visible.txt").write_text("needle", encoding="utf-8")
    blocked = tmp_path / ".venv"
    blocked.mkdir()
    (blocked / "hidden.txt").write_text("needle", encoding="utf-8")
    sessions = tmp_path / ".neil-agent" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "private.json").write_text("needle", encoding="utf-8")
    tools = FileSystemTools(tmp_path)

    result = tools.search_text("needle")

    assert "visible.txt:1" in result
    assert "hidden.txt" not in result
    assert "private.json" not in result
    with pytest.raises(ToolError, match="受保护"):
        tools.read_file(".neil-agent/sessions/private.json")


def test_write_tools_cannot_modify_sensitive_or_outside_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("TOKEN=secret", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    registry = ToolRegistry()
    FileSystemTools(workspace).register(registry)

    env_result = registry.execute(
        ToolCall(
            id="call-env",
            name="write_file",
            arguments={"path": ".env", "content": "changed"},
        ),
        approved=True,
        approved_preview="approved preview",
    )
    outside_result = registry.execute(
        ToolCall(
            id="call-outside",
            name="write_file",
            arguments={"path": "../outside.txt", "content": "changed"},
        ),
        approved=True,
        approved_preview="approved preview",
    )

    assert env_result.is_error is True
    assert outside_result.is_error is True
    assert (workspace / ".env").read_text(encoding="utf-8") == "TOKEN=secret"
    assert outside.read_text(encoding="utf-8") == "outside"


def test_atomic_write_failure_preserves_original_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "important.txt"
    target.write_text("original", encoding="utf-8")
    registry = ToolRegistry()
    FileSystemTools(tmp_path).register(registry)
    call = ToolCall(
        id="call-write",
        name="write_file",
        arguments={"path": "important.txt", "content": "changed"},
    )
    preview = registry.preview(call)

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(filesystem_module.os, "replace", fail_replace)
    result = registry.execute(
        call,
        approved=True,
        approved_preview=preview.content,
    )

    assert result.is_error is True
    assert "原文件保持不变" in result.content
    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".neil-agent-*.tmp")) == []
