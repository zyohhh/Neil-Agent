"""Tests for Git output redaction."""

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
