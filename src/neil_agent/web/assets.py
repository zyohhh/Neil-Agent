"""Integrity checks for the packaged Web Workbench frontend."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from hmac import compare_digest
from pathlib import Path, PurePosixPath
from typing import Any

ASSET_MANIFEST = "asset-manifest.json"
ASSET_MANIFEST_SCHEMA = 1
MAX_STATIC_FILES = 128
MAX_STATIC_BYTES = 32 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class StaticBundleError(RuntimeError):
    """The packaged frontend is missing, malformed, or has been modified."""


@dataclass(frozen=True, slots=True)
class StaticBundle:
    """A verified, filesystem-backed frontend bundle."""

    root: Path
    file_count: int
    total_bytes: int


def packaged_static_root() -> Path:
    """Return the frontend directory installed alongside the Python adapter."""

    return Path(__file__).resolve().with_name("static")


def verify_static_bundle(root: Path) -> StaticBundle:
    """Verify a complete bundle against its deterministic SHA-256 manifest."""

    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise StaticBundleError(
            "Web Workbench frontend assets are not installed"
        ) from error
    if not resolved_root.is_dir():
        raise StaticBundleError("Web Workbench frontend asset path is not a directory")

    manifest_path = resolved_root / ASSET_MANIFEST
    if _is_link_or_junction(manifest_path):
        raise StaticBundleError("Web Workbench asset manifest cannot be a link")
    try:
        raw_manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StaticBundleError(
            "Web Workbench asset manifest is unavailable or invalid"
        ) from error

    if not isinstance(raw_manifest, dict) or set(raw_manifest) != {
        "schema_version",
        "files",
    }:
        raise StaticBundleError("Web Workbench asset manifest has an unknown schema")
    if raw_manifest["schema_version"] != ASSET_MANIFEST_SCHEMA:
        raise StaticBundleError("Web Workbench asset manifest version is unsupported")
    expected = raw_manifest["files"]
    if not isinstance(expected, dict) or not 1 <= len(expected) <= MAX_STATIC_FILES:
        raise StaticBundleError("Web Workbench asset manifest file count is invalid")

    expected_files: dict[str, str] = {}
    for relative_name, digest in expected.items():
        if not isinstance(relative_name, str) or not isinstance(digest, str):
            raise StaticBundleError("Web Workbench asset manifest entries are invalid")
        normalized = PurePosixPath(relative_name)
        if (
            not relative_name
            or relative_name == ASSET_MANIFEST
            or normalized.is_absolute()
            or ".." in normalized.parts
            or normalized.as_posix() != relative_name
            or not _SHA256_PATTERN.fullmatch(digest)
        ):
            raise StaticBundleError(
                "Web Workbench asset manifest contains an unsafe entry"
            )
        expected_files[relative_name] = digest
    if "index.html" not in expected_files:
        raise StaticBundleError(
            "Web Workbench asset manifest does not contain index.html"
        )

    actual_files: dict[str, Path] = {}
    for candidate in resolved_root.rglob("*"):
        if _is_link_or_junction(candidate):
            raise StaticBundleError(
                "Web Workbench frontend assets cannot contain links"
            )
        if candidate.is_file() and candidate != manifest_path:
            relative_name = candidate.relative_to(resolved_root).as_posix()
            actual_files[relative_name] = candidate
    if set(actual_files) != set(expected_files):
        raise StaticBundleError(
            "Web Workbench frontend assets do not match the manifest"
        )

    total_bytes = 0
    for relative_name, candidate in actual_files.items():
        try:
            total_bytes += candidate.stat().st_size
            if total_bytes > MAX_STATIC_BYTES:
                raise StaticBundleError(
                    "Web Workbench frontend assets exceed the size limit"
                )
            digest = _sha256(candidate)
        except OSError as error:
            raise StaticBundleError(
                "Web Workbench frontend asset could not be read"
            ) from error
        if not compare_digest(digest, expected_files[relative_name]):
            raise StaticBundleError(
                "Web Workbench frontend asset integrity check failed"
            )

    return StaticBundle(
        root=resolved_root,
        file_count=len(actual_files),
        total_bytes=total_bytes,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())
