"""Redact sensitive paths from read-only Git command output."""

from __future__ import annotations

import re

from .sensitive_paths import is_sensitive_posix_path

_REDACTED_STATUS_SUFFIX = "[redacted sensitive path]"
_REDACTED_DIFF_BODY = "[Content redacted: sensitive path denied by security policy]\n"
_DIFF_HEADER = re.compile(r"(?=^diff --(?:git|cc|combined) )", re.MULTILINE)
_QUOTED_PATH = re.compile(r'"(?:[^"\\]|\\.)*"')
_C_ESCAPES = {
    "a": 7,
    "b": 8,
    "t": 9,
    "n": 10,
    "v": 11,
    "f": 12,
    "r": 13,
    '"': 34,
    "\\": 92,
}


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
        paths = (
            payload.split(" -> ") if "R" in line[:2] or "C" in line[:2] else [payload]
        )
        if any(_is_sensitive_git_path(path) for path in paths):
            lines.append(f"{line[:3]}{_REDACTED_STATUS_SUFFIX}")
            continue
        lines.append(line)
    return "\n".join(lines)


def redact_git_diff_text(text: str) -> str:
    """Remove unified diff hunks for sensitive paths."""

    if not text:
        return text
    parts = _DIFF_HEADER.split(text)
    redacted: list[str] = []
    prefix = parts[0]
    if prefix.strip():
        redacted.append(_REDACTED_DIFF_BODY)
    for chunk in parts[1:]:
        if not chunk:
            continue
        first_line = chunk.split("\n", 1)[0]
        paths = _parse_diff_git_paths(first_line)
        if paths is None or any(is_sensitive_posix_path(path) for path in paths):
            redacted.append(_REDACTED_DIFF_BODY)
            continue
        redacted.append(chunk)
    return "".join(redacted)


def _is_sensitive_git_path(path: str) -> bool:
    normalized = _normalize_git_path(path)
    return normalized is None or is_sensitive_posix_path(normalized)


def _normalize_git_path(path: str) -> str | None:
    stripped = path.strip()
    if not stripped:
        return ""
    if not stripped.startswith('"'):
        return stripped
    if _QUOTED_PATH.fullmatch(stripped) is None:
        return None
    # Git quotes UTF-8 bytes with C escapes, not JSON or shell quoting.
    data = bytearray()
    body = stripped[1:-1]
    index = 0
    while index < len(body):
        character = body[index]
        index += 1
        if character != "\\":
            data.extend(character.encode("utf-8", errors="surrogateescape"))
            continue
        if index == len(body):
            return None
        escaped = body[index]
        if escaped in _C_ESCAPES:
            data.append(_C_ESCAPES[escaped])
            index += 1
        elif re.fullmatch(r"[0-3][0-7]{2}", body[index : index + 3]):
            data.append(int(body[index : index + 3], 8))
            index += 3
        else:
            return None
    return data.decode("utf-8", errors="surrogateescape")


def _parse_diff_git_paths(line: str) -> tuple[str, str] | None:
    for prefix in ("diff --cc ", "diff --combined "):
        if line.startswith(prefix):
            path = _normalize_git_path(line.removeprefix(prefix))
            return None if path is None else (path, path)
    if not line.startswith("diff --git "):
        return None
    payload = line.removeprefix("diff --git ")
    if payload.startswith('"'):
        first = _QUOTED_PATH.match(payload)
        if first is None or not payload[first.end() :].startswith(" "):
            return None
        left, right = first.group(), payload[first.end() + 1 :]
    else:
        # Spaces are legal unquoted filename characters; ambiguous headers fail closed.
        boundaries = list(re.finditer(r' (?=b/|"b/)', payload))
        if len(boundaries) != 1:
            return None
        boundary = boundaries[0].start()
        left, right = payload[:boundary], payload[boundary + 1 :]
    old, new = _normalize_git_path(left), _normalize_git_path(right)
    if (
        old is None
        or new is None
        or not old.startswith("a/")
        or not new.startswith("b/")
    ):
        return None
    return old[2:], new[2:]
