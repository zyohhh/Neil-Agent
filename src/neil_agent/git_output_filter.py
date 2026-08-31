"""Redact sensitive paths from read-only Git command output."""

from __future__ import annotations

import re

from .sensitive_paths import is_sensitive_posix_path

_REDACTED_STATUS_SUFFIX = "[redacted sensitive path]"
_REDACTED_DIFF_BODY = (
    "[Content redacted: sensitive path denied by security policy]\n"
)
_DIFF_GIT_LINE = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")


def redact_git_status_text(text: str) -> str:
    """Replace porcelain/short status lines for sensitive paths with placeholders."""

    if not text:
        return text
    lines: list[str] = []
    for line in text.splitlines():
        if not line or line.startswith("##"):
            lines.append(line)
            continue
        if len(line) < 3:
            lines.append(line)
            continue
        payload = line[3:]
        if " -> " in payload:
            old_path, new_path = payload.split(" -> ", 1)
            if _is_sensitive_git_path(old_path) or _is_sensitive_git_path(new_path):
                lines.append(f"{line[:3]}{_REDACTED_STATUS_SUFFIX}")
                continue
        elif _is_sensitive_git_path(payload):
            lines.append(f"{line[:3]}{_REDACTED_STATUS_SUFFIX}")
            continue
        lines.append(line)
    return "\n".join(lines)


def redact_git_diff_text(text: str) -> str:
    """Remove unified diff hunks for sensitive paths."""

    if not text:
        return text
    parts = re.split(r"(?=^diff --git )", text, flags=re.MULTILINE)
    if not parts:
        return text
    redacted: list[str] = []
    prefix = parts[0]
    if prefix:
        redacted.append(prefix)
    for chunk in parts[1:]:
        if not chunk:
            continue
        first_line = chunk.split("\n", 1)[0]
        paths = _parse_diff_git_paths(first_line)
        if paths is not None and (
            _is_sensitive_git_path(paths[0]) or _is_sensitive_git_path(paths[1])
        ):
            display = paths[0] if paths[0] == paths[1] else f"{paths[0]} -> {paths[1]}"
            redacted.append(
                f"diff --git a/{display} b/{display}\n{_REDACTED_DIFF_BODY}"
            )
            continue
        redacted.append(chunk)
    return "".join(redacted)


def _is_sensitive_git_path(path: str) -> bool:
    normalized = _normalize_git_path(path)
    if not normalized:
        return False
    return is_sensitive_posix_path(normalized)


def _normalize_git_path(path: str) -> str:
    stripped = path.strip()
    if not stripped:
        return ""
    if stripped.startswith('"') and stripped.endswith('"'):
        stripped = stripped[1:-1]
    return stripped.replace("\\", "/")


def _parse_diff_git_paths(line: str) -> tuple[str, str] | None:
    match = _DIFF_GIT_LINE.match(line)
    if match is None:
        return None
    return _normalize_git_path(match.group(1)), _normalize_git_path(match.group(2))
