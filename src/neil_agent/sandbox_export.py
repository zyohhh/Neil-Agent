"""Bounded guest export manifest for post-certification writable workflows.

Phase 1 is preview-only: validate declared relative paths and build a self-hashing
manifest for approval binding. Import into the workspace is implemented separately.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

GUEST_EXPORT_MANIFEST_VERSION: Literal[1] = 1
MAX_EXPORT_FILES = 32
MAX_EXPORT_FILE_BYTES = 1_000_000
MAX_EXPORT_PATH_CHARS = 1_000
MAX_EXPORT_TOTAL_BYTES = 5_000_000

_BLOCKED_DIRECTORIES = frozenset(
    {
        ".git",
        ".neil-agent",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)
_BLOCKED_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
_BLOCKED_FILE_NAMES = frozenset({".git-credentials", ".netrc", ".npmrc", ".pypirc"})
_WINDOWS_PATH_SEP = re.compile(r"[\\/]+")


class GuestExportError(ValueError):
    """A guest export manifest or path binding was rejected."""


class GuestExportFileEntry(BaseModel):
    """One declared workspace-relative export target with content identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, max_length=MAX_EXPORT_PATH_CHARS)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=MAX_EXPORT_FILE_BYTES)

    @model_validator(mode="after")
    def path_must_be_safe_relative(self) -> Self:
        normalized = _normalize_relative_path(self.path)
        object.__setattr__(self, "path", normalized)
        return self


class GuestExportManifest(BaseModel):
    """Self-hashing manifest binding one sandbox run to declared export files."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = GUEST_EXPORT_MANIFEST_VERSION
    run_id: str = Field(min_length=1, max_length=80)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    certification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[GuestExportFileEntry, ...] = Field(min_length=1, max_length=MAX_EXPORT_FILES)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def manifest_digest_must_match(self) -> Self:
        expected = _digest_payload(self.hash_payload())
        if self.manifest_sha256 != expected:
            raise ValueError("guest export manifest digest is invalid")
        paths = [item.path for item in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("guest export manifest paths must be unique")
        total = sum(item.size_bytes for item in self.files)
        if total > MAX_EXPORT_TOTAL_BYTES:
            raise ValueError("guest export manifest exceeds its total byte budget")
        return self

    def hash_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"manifest_sha256"})


def build_guest_export_manifest(
    *,
    run_id: str,
    request_hash: str,
    certification_sha256: str,
    files: tuple[tuple[str, bytes], ...],
) -> GuestExportManifest:
    """Build one canonical manifest from declared relative paths and raw bytes."""

    if not run_id or len(run_id) > 80:
        raise GuestExportError("guest export run identity is invalid")
    if not re.fullmatch(r"^[0-9a-f]{64}$", request_hash):
        raise GuestExportError("guest export request hash is invalid")
    if not re.fullmatch(r"^[0-9a-f]{64}$", certification_sha256):
        raise GuestExportError("guest export certification hash is invalid")
    if not files:
        raise GuestExportError("guest export manifest requires at least one file")
    if len(files) > MAX_EXPORT_FILES:
        raise GuestExportError("guest export manifest exceeds its file count limit")

    entries: list[GuestExportFileEntry] = []
    total_bytes = 0
    seen_paths: set[str] = set()
    for relative_path, payload in files:
        normalized = _normalize_relative_path(relative_path)
        if normalized in seen_paths:
            raise GuestExportError("guest export manifest contains duplicate paths")
        seen_paths.add(normalized)
        if not 1 <= len(payload) <= MAX_EXPORT_FILE_BYTES:
            raise GuestExportError("guest export file size is outside its bound")
        total_bytes += len(payload)
        if total_bytes > MAX_EXPORT_TOTAL_BYTES:
            raise GuestExportError("guest export manifest exceeds its total byte budget")
        entries.append(
            GuestExportFileEntry(
                path=normalized,
                sha256=sha256(payload).hexdigest(),
                size_bytes=len(payload),
            )
        )

    provisional = GuestExportManifest.model_construct(
        version=GUEST_EXPORT_MANIFEST_VERSION,
        run_id=run_id,
        request_hash=request_hash,
        certification_sha256=certification_sha256,
        files=tuple(entries),
        manifest_sha256="0" * 64,
    )
    return provisional.model_copy(
        update={"manifest_sha256": _digest_payload(provisional.hash_payload())}
    )


def format_guest_export_preview(manifest: GuestExportManifest) -> str:
    """Render a bounded, approval-friendly preview without file bodies."""

    lines = [
        "GUEST EXPORT MANIFEST (PREVIEW ONLY)",
        f"run_id={manifest.run_id}",
        f"request_hash={manifest.request_hash}",
        f"certification_sha256={manifest.certification_sha256}",
        f"manifest_sha256={manifest.manifest_sha256}",
        f"file_count={len(manifest.files)}",
        "",
    ]
    for index, entry in enumerate(manifest.files, start=1):
        lines.append(
            f"{index:02d}. {entry.path} · {entry.size_bytes} bytes · "
            f"sha256={entry.sha256}"
        )
    lines.append("")
    lines.append("IMPORT INTO WORKSPACE IS NOT ENABLED IN THIS PHASE.")
    return "\n".join(lines)


def _normalize_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GuestExportError("guest export path is empty")
    candidate = value.strip().replace("\\", "/")
    if candidate.startswith("/") or _WINDOWS_PATH_SEP.search(candidate):
        if "\\" in value or value.startswith("/"):
            raise GuestExportError("guest export path must be workspace-relative")
    relative = PurePosixPath(candidate)
    if ".." in relative.parts or relative.is_absolute():
        raise GuestExportError("guest export path escapes the workspace")
    if len(str(relative)) > MAX_EXPORT_PATH_CHARS:
        raise GuestExportError("guest export path exceeds its length limit")
    normalized = relative.as_posix()
    parts = PurePosixPath(normalized).parts
    if any(part.lower() in _BLOCKED_DIRECTORIES for part in parts):
        raise GuestExportError("guest export path targets a blocked directory")
    name = parts[-1] if parts else normalized
    lowered = name.lower()
    if (
        lowered in _BLOCKED_FILE_NAMES
        or lowered == ".env"
        or lowered.startswith(".env.")
        or Path(name).suffix.lower() in _BLOCKED_SUFFIXES
    ):
        raise GuestExportError("guest export path targets a sensitive file")
    return normalized


def _digest_payload(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
