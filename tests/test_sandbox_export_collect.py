"""Tests for declared guest export collection."""

from __future__ import annotations

from pathlib import Path

import pytest

from neil_agent.sandbox_export import GuestExportError
from neil_agent.sandbox_export_collect import (
    collect_declared_guest_exports,
    normalize_export_paths,
)


def test_normalize_export_paths_canonicalizes_and_rejects_duplicates() -> None:
    assert normalize_export_paths(["out/a.txt", "out/b.txt"]) == (
        "out/a.txt",
        "out/b.txt",
    )
    with pytest.raises(GuestExportError, match="duplicate"):
        normalize_export_paths(["out/a.txt", "out/a.txt"])


def test_collect_declared_guest_exports_reads_only_declared_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")

    contents = collect_declared_guest_exports(
        tmp_path,
        ("out/a.txt",),
    )

    assert contents == {"out/a.txt": b"alpha"}


def test_collect_declared_guest_exports_rejects_unknown_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "extra.txt").write_text("x", encoding="utf-8")
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")

    with pytest.raises(GuestExportError, match="undeclared"):
        collect_declared_guest_exports(tmp_path, ("out/a.txt",))
