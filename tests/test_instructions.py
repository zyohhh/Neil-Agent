"""Tests for bounded workspace-root project instruction loading."""

from pathlib import Path

import pytest

from neil_agent.instructions import (
    MAX_INSTRUCTIONS_FILE_BYTES,
    load_project_instructions,
)


def test_loads_utf8_root_agents_file_and_hides_content_from_repr(
    tmp_path: Path,
) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_bytes(b"\xef\xbb\xbf# Rules\r\n\r\nUse pytest.\r\n")

    instructions = load_project_instructions(tmp_path)

    assert instructions.active is True
    assert instructions.source == source
    assert instructions.content == "# Rules\n\nUse pytest."
    assert instructions.char_count == len(instructions.content)
    assert "Use pytest" not in repr(instructions)
    assert "BEGIN PROJECT INSTRUCTIONS" in instructions.prompt_section()


def test_missing_and_empty_instruction_files_are_inactive(tmp_path: Path) -> None:
    missing = load_project_instructions(tmp_path)
    (tmp_path / "AGENTS.md").write_text(" \n\t", encoding="utf-8")
    empty = load_project_instructions(tmp_path)

    assert missing.status == "missing"
    assert empty.status == "empty"
    assert missing.prompt_section() == ""
    assert empty.prompt_section() == ""


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"\xff\xfe", "UTF-8"),
        ("hidden\u200brule".encode(), "控制或格式字符"),
        (b"x" * (MAX_INSTRUCTIONS_FILE_BYTES + 1), "字节上限"),
    ],
    ids=("invalid-utf8", "control-character", "oversized"),
)
def test_rejects_unsafe_instruction_content(
    tmp_path: Path,
    payload: bytes,
    reason: str,
) -> None:
    (tmp_path / "AGENTS.md").write_bytes(payload)

    instructions = load_project_instructions(tmp_path)

    assert instructions.status == "invalid"
    assert reason in instructions.reason
    assert instructions.content == ""


def test_rejects_instruction_symlink_when_supported(tmp_path: Path) -> None:
    target = tmp_path / "outside.md"
    target.write_text("external rules", encoding="utf-8")
    source = tmp_path / "AGENTS.md"
    try:
        source.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    instructions = load_project_instructions(tmp_path)

    assert instructions.status == "invalid"
    assert "符号链接" in instructions.reason
