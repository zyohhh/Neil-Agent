"""Regression tests for bounded Windows Sandbox handle leases."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from neil_agent.errors import SandboxError
from neil_agent.sandbox_lease import BoundedTreeLease


@pytest.mark.skipif(os.name != "nt", reason="requires Windows share modes")
def test_sealed_lease_blocks_write_delete_and_rename(tmp_path: Path) -> None:
    root = (tmp_path / "sealed").resolve()
    root.mkdir()
    payload = root / "result.json"
    payload.write_bytes(b"sealed")

    lease = BoundedTreeLease(
        root,
        expected_names=frozenset({"result.json"}),
        max_entries=1,
        max_total_bytes=64,
    )
    try:
        with pytest.raises(OSError):
            payload.write_bytes(b"forged")
        with pytest.raises(OSError):
            payload.unlink()
        with pytest.raises(OSError):
            payload.rename(root / "renamed.json")
        lease.validate()
        assert lease.read_file("result.json", 64) == b"sealed"
    finally:
        lease.close()

    payload.write_bytes(b"released")
    assert payload.read_bytes() == b"released"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows share modes")
def test_sealed_lease_detects_new_directory_entry(tmp_path: Path) -> None:
    root = (tmp_path / "sealed").resolve()
    root.mkdir()
    (root / "fixed.txt").write_bytes(b"fixed")

    with BoundedTreeLease(
        root,
        max_entries=4,
        max_total_bytes=64,
    ) as lease:
        (root / "late.txt").write_bytes(b"late")
        with pytest.raises(SandboxError, match="entries changed"):
            lease.validate()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows share modes")
def test_writable_root_lease_allows_export_but_blocks_root_replacement(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "export").resolve()
    root.mkdir()

    with BoundedTreeLease(
        root,
        writable_root=True,
        max_entries=1,
        max_total_bytes=64,
    ) as lease:
        (root / "result.json").write_bytes(b"result")
        with pytest.raises(OSError):
            root.rename(tmp_path / "replaced-export")
        lease.validate()


def test_lease_rejects_entry_and_byte_limit_overflow(tmp_path: Path) -> None:
    root = (tmp_path / "bounded").resolve()
    root.mkdir()
    (root / "one").write_bytes(b"1234")
    (root / "two").write_bytes(b"5678")

    with pytest.raises(SandboxError, match="entry limit"):
        BoundedTreeLease(root, max_entries=1, max_total_bytes=64)
    with pytest.raises(SandboxError, match="byte limit"):
        BoundedTreeLease(root, max_entries=2, max_total_bytes=7)
