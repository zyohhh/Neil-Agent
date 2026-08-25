"""Collect and validate declared guest export files from a sealed export root."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from .sandbox_export import (
    MAX_EXPORT_FILES,
    MAX_EXPORT_TOTAL_BYTES,
    GuestExportError,
    _normalize_relative_path,
)

GUEST_RESULT_FILENAME = "result.json"


def normalize_export_paths(paths: object) -> tuple[str, ...]:
    """Validate and canonicalize declared workspace-relative export paths."""

    if paths is None:
        return ()
    if paths == () or paths == []:
        return ()
    if isinstance(paths, tuple):
        items = list(paths)
    elif isinstance(paths, list):
        items = paths
    else:
        raise GuestExportError("guest export paths must be an array")
    if len(items) > MAX_EXPORT_FILES:
        raise GuestExportError("guest export manifest exceeds its file count limit")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise GuestExportError("guest export path must be a string")
        path = _normalize_relative_path(item)
        if path in seen:
            raise GuestExportError("guest export manifest contains duplicate paths")
        seen.add(path)
        normalized.append(path)
    return tuple(normalized)


def collect_declared_guest_exports(
    export_root: Path,
    export_paths: tuple[str, ...],
    *,
    result_filename: str = GUEST_RESULT_FILENAME,
) -> dict[str, bytes]:
    """Read exactly the declared export files and reject unknown siblings."""

    if not export_paths:
        return {}
    root = export_root.resolve(strict=True)
    if not root.is_dir():
        raise GuestExportError("guest export root is unavailable")

    expected = set(export_paths)
    discovered: dict[str, bytes] = {}
    total_bytes = 0
    for current_path, _directory_names, file_names in os.walk(root):
        current = Path(current_path)
        for name in file_names:
            file_path = current / name
            try:
                file_stat = file_path.lstat()
                resolved = file_path.resolve(strict=True)
            except OSError as error:
                raise GuestExportError("guest export file is unreadable") from error
            if (
                file_path.is_symlink()
                or not stat.S_ISREG(file_stat.st_mode)
                or resolved != file_path
            ):
                raise GuestExportError("guest export file is not a safe regular file")
            relative = resolved.relative_to(root).as_posix()
            if relative == result_filename:
                continue
            if relative not in expected:
                raise GuestExportError("guest export contains an undeclared file")
            if relative in discovered:
                raise GuestExportError("guest export manifest contains duplicate paths")
            payload = resolved.read_bytes()
            total_bytes += len(payload)
            if total_bytes > MAX_EXPORT_TOTAL_BYTES:
                raise GuestExportError("guest export manifest exceeds its total byte budget")
            discovered[relative] = payload

    missing = expected.difference(discovered)
    if missing:
        raise GuestExportError("guest export is missing a declared file")
    return discovered
