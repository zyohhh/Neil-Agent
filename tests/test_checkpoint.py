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
    assert restored == "已恢复最近任务检查点：1 个文件（恢复 1，删除 0）"
    assert target.read_text(encoding="utf-8") == "original\n"
    assert tools.checkpoints.count == 0


def test_restore_deletes_a_file_created_by_agent(tmp_path: Path) -> None:
    tools = FileSystemTools(tmp_path)
    tools.write_file("created.txt", "new\n")

    prepared = tools.prepare_latest_restore()
    result = tools.apply_latest_restore(prepared)

    assert prepared.deletes_created_file is True
    assert "+++ /dev/null" in prepared.preview
    assert result == "已恢复最近任务检查点：1 个文件（恢复 0，删除 1）"
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


def test_one_agent_task_groups_multiple_files_and_repeated_edits(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    first.write_text("first-original", encoding="utf-8")
    tools = FileSystemTools(tmp_path)
    task_id = tools.checkpoints.begin_task()

    first_result = tools.write_file("first.txt", "first-intermediate")
    second_result = tools.write_file("first.txt", "first-final")
    created_result = tools.write_file("created.txt", "created")
    checkpoint = tools.checkpoints.finish_task(task_id)

    assert checkpoint is not None
    assert checkpoint.file_count == 2
    assert [edit.path for edit in checkpoint.edits] == ["first.txt", "created.txt"]
    assert all(
        task_id in result for result in (first_result, second_result, created_result)
    )
    assert tools.checkpoints.count == 1

    prepared = tools.prepare_latest_restore()
    result = tools.apply_latest_restore(prepared)

    assert prepared.file_count == 2
    assert first.read_text(encoding="utf-8") == "first-original"
    assert not (tmp_path / "created.txt").exists()
    assert result == "已恢复最近任务检查点：2 个文件（恢复 1，删除 1）"


def test_task_drops_a_path_that_returns_to_its_original_content(
    tmp_path: Path,
) -> None:
    target = tmp_path / "example.txt"
    target.write_text("original", encoding="utf-8")
    tools = FileSystemTools(tmp_path)
    task_id = tools.checkpoints.begin_task()

    tools.write_file("example.txt", "temporary")
    tools.write_file("example.txt", "original")
    checkpoint = tools.checkpoints.finish_task(task_id)

    assert checkpoint is None
    assert tools.checkpoints.count == 0
    assert target.read_text(encoding="utf-8") == "original"


def test_task_checkpoint_capacity_failure_happens_before_write(
    tmp_path: Path,
) -> None:
    target = tmp_path / "large.txt"
    target.write_text("1234", encoding="utf-8")
    history = FileCheckpointHistory(max_content_chars=3)
    tools = FileSystemTools(tmp_path, checkpoints=history)

    with pytest.raises(ToolError, match="容量不足.*写入前拒绝"):
        tools.write_file("large.txt", "changed")

    assert target.read_text(encoding="utf-8") == "1234"
    assert history.count == 0


def test_task_file_count_limit_rejects_only_the_next_write(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    history = FileCheckpointHistory(max_files_per_checkpoint=1)
    tools = FileSystemTools(tmp_path, checkpoints=history)
    task_id = history.begin_task()

    tools.write_file("first.txt", "changed one")
    with pytest.raises(ToolError, match="文件数量.*写入前拒绝"):
        tools.write_file("second.txt", "changed two")
    checkpoint = history.finish_task(task_id)

    assert checkpoint is not None
    assert checkpoint.file_count == 1
    assert first.read_text(encoding="utf-8") == "changed one"
    assert second.read_text(encoding="utf-8") == "two"


def test_task_result_capacity_bounds_created_file_rollback_state(
    tmp_path: Path,
) -> None:
    history = FileCheckpointHistory(max_content_chars=5)
    tools = FileSystemTools(tmp_path, checkpoints=history)
    task_id = history.begin_task()

    tools.write_file("first.txt", "123")
    with pytest.raises(ToolError, match="容量不足.*写入前拒绝"):
        tools.write_file("second.txt", "456")
    checkpoint = history.finish_task(task_id)

    assert checkpoint is not None
    assert checkpoint.file_count == 1
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "123"
    assert not (tmp_path / "second.txt").exists()


def test_multi_file_restore_preflights_every_file_before_mutation(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    tools = FileSystemTools(tmp_path)
    task_id = tools.checkpoints.begin_task()
    tools.write_file("first.txt", "changed one")
    tools.write_file("second.txt", "changed two")
    tools.checkpoints.finish_task(task_id)
    prepared = tools.prepare_latest_restore()
    second.write_text("external", encoding="utf-8")

    with pytest.raises(ToolError, match="批准后任务文件 second.txt 发生变化"):
        tools.apply_latest_restore(prepared)

    assert first.read_text(encoding="utf-8") == "changed one"
    assert second.read_text(encoding="utf-8") == "external"
    assert tools.checkpoints.count == 1


def test_multi_file_restore_rolls_back_prior_files_when_apply_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    tools = FileSystemTools(tmp_path)
    task_id = tools.checkpoints.begin_task()
    tools.write_file("first.txt", "changed one")
    tools.write_file("second.txt", "changed two")
    tools.checkpoints.finish_task(task_id)
    prepared = tools.prepare_latest_restore()
    atomic_write = tools._atomic_write

    def fail_second_restore(file_path: Path, content: str) -> None:
        if file_path.name == "second.txt" and content == "two":
            raise ToolError("injected restore failure")
        atomic_write(file_path, content)

    monkeypatch.setattr(tools, "_atomic_write", fail_second_restore)

    with pytest.raises(ToolError, match="已回滚本次恢复操作"):
        tools.apply_latest_restore(prepared)

    assert first.read_text(encoding="utf-8") == "changed one"
    assert second.read_text(encoding="utf-8") == "changed two"
    assert tools.checkpoints.count == 1


def test_failed_restore_recreates_an_already_deleted_task_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("original", encoding="utf-8")
    tools = FileSystemTools(tmp_path)
    task_id = tools.checkpoints.begin_task()
    tools.write_file("created.txt", "created")
    tools.write_file("existing.txt", "changed")
    tools.checkpoints.finish_task(task_id)
    prepared = tools.prepare_latest_restore()
    atomic_write = tools._atomic_write

    def fail_existing_restore(file_path: Path, content: str) -> None:
        if file_path.name == "existing.txt" and content == "original":
            raise ToolError("injected restore failure")
        atomic_write(file_path, content)

    monkeypatch.setattr(tools, "_atomic_write", fail_existing_restore)

    with pytest.raises(ToolError, match="已回滚本次恢复操作"):
        tools.apply_latest_restore(prepared)

    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created"
    assert existing.read_text(encoding="utf-8") == "changed"
    assert tools.checkpoints.count == 1


def test_multi_file_restore_reports_incomplete_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    tools = FileSystemTools(tmp_path)
    task_id = tools.checkpoints.begin_task()
    tools.write_file("first.txt", "changed one")
    tools.write_file("second.txt", "changed two")
    tools.checkpoints.finish_task(task_id)
    prepared = tools.prepare_latest_restore()
    atomic_write = tools._atomic_write

    def fail_restore_and_rollback(file_path: Path, content: str) -> None:
        if (
            file_path.name == "second.txt"
            and content == "two"
            or file_path.name == "first.txt"
            and content == "changed one"
        ):
            raise ToolError("injected write failure")
        atomic_write(file_path, content)

    monkeypatch.setattr(tools, "_atomic_write", fail_restore_and_rollback)

    with pytest.raises(ToolError, match="回滚不完整.*使用 Git"):
        tools.apply_latest_restore(prepared)

    assert first.read_text(encoding="utf-8") == "one"
    assert second.read_text(encoding="utf-8") == "changed two"
    assert tools.checkpoints.count == 1
