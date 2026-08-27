"""Single secret-path denylist shared by host tools, Git, Web, and sandbox."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path, PurePosixPath

BLOCKED_DIRECTORIES = frozenset(
    {
        ".agents",
        ".aws",
        ".azure",
        ".codex",
        ".docker",
        ".git",
        ".gnupg",
        ".kube",
        ".mypy_cache",
        ".neil-agent",
        ".npm-cache",
        ".pytest_cache",
        ".ruff_cache",
        ".ssh",
        ".uv-cache",
        ".venv",
        "__pycache__",
        "appdata",
        "node_modules",
    }
)
BLOCKED_FILE_NAMES = frozenset(
    {
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "application_default_credentials.json",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
BLOCKED_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
ENV_EXAMPLE_NAME = ".env.example"

# Compatibility aliases for Git/Web call sites that historically used these names.
BLOCKED_GIT_DIRECTORIES = BLOCKED_DIRECTORIES
BLOCKED_GIT_FILE_NAMES = BLOCKED_FILE_NAMES
BLOCKED_GIT_SUFFIXES = BLOCKED_SUFFIXES


def is_blocked_directory_name(name: str) -> bool:
    """Return True when a path component is a protected directory name."""

    return name.casefold() in BLOCKED_DIRECTORIES


def is_sensitive_file_name(name: str) -> bool:
    """Return True when a file basename is a credential or env secret."""

    lowered = name.casefold()
    if lowered == ENV_EXAMPLE_NAME:
        return False
    if lowered == ".env" or lowered.startswith(".env."):
        return True
    if lowered in BLOCKED_FILE_NAMES:
        return True
    return Path(lowered).suffix in BLOCKED_SUFFIXES


def is_sensitive_entry_name(name: str) -> bool:
    """Return True when a listing name is a protected directory or secret file."""

    return is_blocked_directory_name(name) or is_sensitive_file_name(name)


def is_sensitive_relative_path(parts: Sequence[str]) -> bool:
    """Return True when any workspace-relative component is protected."""

    if not parts:
        return False
    if any(is_blocked_directory_name(part) for part in parts):
        return True
    return is_sensitive_file_name(parts[-1])


def is_sensitive_posix_path(value: str) -> bool:
    """Return True when a POSIX-style relative path is protected."""

    normalized = value.replace("\\", "/").strip("/")
    if not normalized:
        return False
    return is_sensitive_relative_path(PurePosixPath(normalized).parts)
