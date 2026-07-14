"""Tests for filesystem workspace and sensitive-file boundaries."""

from pathlib import Path

import pytest

from neil_agent.tools.filesystem import FileSystemTools
from neil_agent.tools.registry import ToolError


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
    tools = FileSystemTools(tmp_path)

    result = tools.search_text("needle")

    assert "visible.txt:1" in result
    assert "hidden.txt" not in result
