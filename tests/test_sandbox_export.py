"""Tests for guest export manifest preview binding."""

from __future__ import annotations

import pytest

from neil_agent.sandbox_export import (
    GuestExportError,
    GuestExportManifest,
    build_guest_export_manifest,
    format_guest_export_preview,
)


def test_build_guest_export_manifest_hashes_and_binds_run_identity() -> None:
    manifest = build_guest_export_manifest(
        run_id="run-01",
        request_hash="a" * 64,
        certification_sha256="b" * 64,
        files=(("out/result.txt", b"hello"),),
    )

    assert manifest.version == 1
    assert manifest.files[0].path == "out/result.txt"
    assert manifest.files[0].size_bytes == 5
    GuestExportManifest.model_validate(manifest.model_dump(mode="json"))


def test_guest_export_preview_is_body_free() -> None:
    manifest = build_guest_export_manifest(
        run_id="run-01",
        request_hash="a" * 64,
        certification_sha256="b" * 64,
        files=(("out/result.txt", b"secret-body"),),
    )

    preview = format_guest_export_preview(manifest)

    assert "secret-body" not in preview
    assert "PREVIEW ONLY" in preview
    assert "out/result.txt" in preview


def test_guest_export_rejects_escape_and_sensitive_paths() -> None:
    with pytest.raises(GuestExportError, match="escapes"):
        build_guest_export_manifest(
            run_id="run-01",
            request_hash="a" * 64,
            certification_sha256="b" * 64,
            files=(("../escape.txt", b"x"),),
        )
    with pytest.raises(GuestExportError, match="sensitive"):
        build_guest_export_manifest(
            run_id="run-01",
            request_hash="a" * 64,
            certification_sha256="b" * 64,
            files=((".env", b"x"),),
        )
    with pytest.raises(GuestExportError, match="blocked"):
        build_guest_export_manifest(
            run_id="run-01",
            request_hash="a" * 64,
            certification_sha256="b" * 64,
            files=((".git/config", b"x"),),
        )
