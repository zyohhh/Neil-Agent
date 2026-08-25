"""Tests for guest export import transactions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from neil_agent.errors import ToolError
from neil_agent.sandbox_approval import GuestExportImportBinding
from neil_agent.sandbox_export import build_guest_export_manifest
from neil_agent.tools.filesystem import FileSystemTools


def _manifest_and_contents(
    *,
    files: tuple[tuple[str, bytes], ...],
) -> tuple[object, dict[str, bytes]]:
    manifest = build_guest_export_manifest(
        run_id="run-01",
        request_hash="a" * 64,
        certification_sha256="b" * 64,
        files=files,
    )
    return manifest, {path: payload for path, payload in files}


def test_guest_export_import_binding_digest_is_stable() -> None:
    manifest, _contents = _manifest_and_contents(
        files=(("out/result.txt", b"hello"),),
    )
    binding = GuestExportImportBinding.from_manifest(manifest)
    assert binding.digest == GuestExportImportBinding.from_manifest(manifest).digest
    assert binding.approval_binding.kind == "guest-export-import"


def test_guest_export_import_create_and_replace(tmp_path: Path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("before\n", encoding="utf-8")
    tools = FileSystemTools(tmp_path)
    manifest, contents = _manifest_and_contents(
        files=(
            ("existing.txt", b"after\n"),
            ("new/file.txt", b"created\n"),
        ),
    )
    (tmp_path / "new").mkdir()

    prepared = tools.prepare_guest_export_import(manifest, contents)
    assert prepared.create_count == 1
    assert prepared.replace_count == 1
    assert "Guest export import approval" in prepared.preview
    assert "+after" in prepared.preview
    assert "+created" in prepared.preview

    result = tools.apply_guest_export_import(manifest, prepared)

    assert result == "已导入 guest export：2 个文件（新建 1，替换 1）"
    assert existing.read_text(encoding="utf-8") == "after\n"
    assert (tmp_path / "new" / "file.txt").read_text(encoding="utf-8") == "created\n"


def test_guest_export_import_rejects_non_utf8(tmp_path: Path) -> None:
    tools = FileSystemTools(tmp_path)
    manifest, contents = _manifest_and_contents(
        files=(("out.bin", b"\xff\xfe"),),
    )

    with pytest.raises(ToolError, match="UTF-8"):
        tools.prepare_guest_export_import(manifest, contents)


def test_guest_export_import_rejects_workspace_change_after_preview(
    tmp_path: Path,
) -> None:
    target = tmp_path / "example.txt"
    target.write_text("before\n", encoding="utf-8")
    tools = FileSystemTools(tmp_path)
    manifest, contents = _manifest_and_contents(
        files=(("example.txt", b"after\n"),),
    )
    prepared = tools.prepare_guest_export_import(manifest, contents)
    target.write_text("changed after approval\n", encoding="utf-8")

    with pytest.raises(ToolError, match="批准后"):
        tools.apply_guest_export_import(manifest, prepared)

    assert target.read_text(encoding="utf-8") == "changed after approval\n"


def test_guest_export_import_rolls_back_partial_failure(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("keep\n", encoding="utf-8")
    second.write_text("replace-me\n", encoding="utf-8")
    tools = FileSystemTools(tmp_path)
    manifest, contents = _manifest_and_contents(
        files=(
            ("first.txt", b"updated-first\n"),
            ("second.txt", b"updated-second\n"),
        ),
    )
    prepared = tools.prepare_guest_export_import(manifest, contents)
    original_write = tools._atomic_write
    calls = {"count": 0}

    def flaky_write(file_path: Path, content: str) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise ToolError("simulated write failure")
        original_write(file_path, content)

    with patch.object(tools, "_atomic_write", side_effect=flaky_write):
        with pytest.raises(ToolError, match="已回滚"):
            tools.apply_guest_export_import(manifest, prepared)

    assert first.read_text(encoding="utf-8") == "keep\n"
    assert second.read_text(encoding="utf-8") == "replace-me\n"
