"""Tests for minimal in-process file edit checkpoints."""

import os
from pathlib import Path

import pytest

from neil_agent.checkpoint import FileCheckpointHistory
from neil_agent.errors import ToolError
from neil_agent.tools.filesystem import FileSystemTools


def test_existing_file_edit_can_be_previewed_and_restored(tmp_path: Path) -> None:
    target = tmp_path / "example.txt"
    target.write_text("original\n", encoding="utf-8")
    tools = FileSystemTools(tmp_path)

    result = tools.write_file("example.txt", "changed\n")
    prepared = tools.prepare_latest_restore()
    restored = tools.apply_latest_restore(prepared)

    assert "恢复检查点" in result
    assert "-changed" in prepared.preview
    assert "+original" in prepared.preview
    assert restored == "已恢复文件原内容：example.txt"
    assert target.read_text(encoding="utf-8") == "original\n"
    assert tools.checkpoints.count == 0


def test_restore_deletes_a_file_created_by_agent(tmp_path: Path) -> None:
    tools = FileSystemTools(tmp_path)
    tools.write_file("created.txt", "new\n")

    prepared = tools.prepare_latest_restore()
    result = tools.apply_latest_restore(prepared)

    assert prepared.deletes_created_file is True
    assert "+++ /dev/null" in prepared.preview
    assert result == "已删除 Agent 新建文件：created.txt"
    assert not (tmp_path / "created.txt").exists()


def test_external_change_before_preview_refuses_restore(tmp_path: Path) -> None:
    target = tmp_path / "example.txt"
    target.write_text("original", encoding="utf-8")
    tools = FileSystemTools(tmp_path)
    tools.write_file("example.txt", "agent edit")
    target.write_text("external edit", encoding="utf-8")

    with pytest.raises(ToolError, match="外部变化"):
        tools.prepare_latest_restore()

    assert target.read_text(encoding="utf-8") == "external edit"
    assert tools.checkpoints.count == 1


def test_change_after_restore_preview_is_rechecked(tmp_path: Path) -> None:
    target = tmp_path / "example.txt"
    target.write_text("original", encoding="utf-8")
    tools = FileSystemTools(tmp_path)
    tools.write_file("example.txt", "agent edit")
    prepared = tools.prepare_latest_restore()
    target.write_text("changed after approval", encoding="utf-8")

    with pytest.raises(ToolError, match="批准后"):
        tools.apply_latest_restore(prepared)

    assert target.read_text(encoding="utf-8") == "changed after approval"
    assert tools.checkpoints.count == 1


def test_restore_rejects_path_replaced_by_symlink_when_supported(
    tmp_path: Path,
) -> None:
    created = tmp_path / "created.txt"
    other = tmp_path / "other.txt"
    other.write_text("agent content", encoding="utf-8")
    tools = FileSystemTools(tmp_path)
    tools.write_file("created.txt", "agent content")
    created.unlink()
    try:
        os.symlink(other, created)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不允许创建测试符号链接")

    with pytest.raises(ToolError, match="路径.*外部变化"):
        tools.prepare_latest_restore()

    assert other.read_text(encoding="utf-8") == "agent content"
    assert tools.checkpoints.count == 1


def test_checkpoint_history_evicts_old_content_within_bounds() -> None:
    identifiers = iter(("one", "two", "three"))
    history = FileCheckpointHistory(
        max_entries=3,
        max_content_chars=5,
        id_factory=lambda: next(identifiers),
    )

    history.record("one.txt", "1111", "a")
    history.record("two.txt", "2222", "b")
    latest = history.record("three.txt", "3", "c")

    assert history.count == 2
    assert history.latest == latest
