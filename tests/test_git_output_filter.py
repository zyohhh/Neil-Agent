"""Tests for Git output redaction."""

import pytest

from neil_agent.git_output_filter import redact_git_diff_text, redact_git_status_text


def test_redact_git_status_text_hides_sensitive_paths() -> None:
    raw = "\n".join(
        [
            "## main...origin/main",
            " M src/agent.py",
            " M .env",
            "?? .ssh/id_rsa",
            "RM secrets.txt -> .env.local",
        ]
    )
    redacted = redact_git_status_text(raw)
    assert "## main...origin/main" in redacted
    assert "src/agent.py" in redacted
    assert ".env" not in redacted.splitlines()[2]
    assert "id_rsa" not in redacted
    assert "[redacted sensitive path]" in redacted


def test_redact_git_diff_text_strips_sensitive_hunks() -> None:
    raw = (
        "diff --git a/src/agent.py b/src/agent.py\n"
        "index 111..222\n"
        "--- a/src/agent.py\n"
        "+++ b/src/agent.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/.env b/.env\n"
        "index 333..444\n"
        "--- a/.env\n"
        "+++ b/.env\n"
        "@@ -1 +1 @@\n"
        "-SECRET=1\n"
        "+SECRET=2\n"
    )
    redacted = redact_git_diff_text(raw)
    assert "src/agent.py" in redacted
    assert "+new" in redacted
    assert "SECRET=1" not in redacted
    assert "SECRET=2" not in redacted
    assert "Content redacted" in redacted


@pytest.mark.parametrize("header", [
    'diff --git "a/\\344\\270\\255/private.pem" "b/\\344\\270\\255/private.pem"',
    'diff --git "a/.ssh/a\\tb" "b/.ssh/a\\tb"',
    'diff --git "a/\\056env" "b/\\056env"',
    "diff --cc .env",
    "diff --combined .aws/credentials",
    "diff --git .env .env",
    'diff --git "a/invalid\\q" "b/invalid\\q"',
])
def test_unrecognized_and_sensitive_diff_headers_fail_closed(header: str) -> None:
    raw = f"{header}\n@@ -1 +1 @@\n-SYNTHETIC_SECRET\n+NEW_SECRET\n"
    redacted = redact_git_diff_text(raw)
    assert "SECRET" not in redacted
    assert "redacted" in redacted


@pytest.mark.parametrize("path", ['"\\056env"', '".ssh/\\344\\270\\255.txt"', '"key\\tname.pem"'])
def test_c_quoted_status_paths_are_decoded_before_checking(path: str) -> None:
    assert redact_git_status_text(f" M {path}") == " M [redacted sensitive path]"


def test_safe_quoted_and_space_paths_remain_readable() -> None:
    for path in ['"a/\\344\\270\\255.txt" "b/\\344\\270\\255.txt"', 'a/space name.txt b/space name.txt']:
        raw = f"diff --git {path}\n@@ -1 +1 @@\n-old\n+visible change\n"
        assert redact_git_diff_text(raw) == raw
