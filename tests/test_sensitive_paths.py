"""Tests for the shared secret-path denylist."""

from __future__ import annotations

from neil_agent.sensitive_paths import (
    is_blocked_directory_name,
    is_sensitive_entry_name,
    is_sensitive_file_name,
    is_sensitive_posix_path,
    is_sensitive_relative_path,
)


def test_shared_denylist_covers_snapshot_secrets_and_allows_env_example() -> None:
    assert is_blocked_directory_name(".ssh")
    assert is_blocked_directory_name("AppData")
    assert is_sensitive_file_name("id_rsa")
    assert is_sensitive_file_name("credentials.json")
    assert is_sensitive_file_name(".env")
    assert is_sensitive_file_name(".env.local")
    assert is_sensitive_file_name("private.pem")
    assert not is_sensitive_file_name(".env.example")
    assert not is_sensitive_file_name("agent.py")
    assert is_sensitive_posix_path(".ssh/id_rsa")
    assert is_sensitive_posix_path(".aws/credentials")
    assert is_sensitive_relative_path(("src", "id_ed25519"))
    assert is_sensitive_entry_name("node_modules")
    assert not is_sensitive_posix_path("src/neil_agent/agent.py")
