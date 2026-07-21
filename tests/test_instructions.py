"""Tests for bounded workspace-root project instruction loading."""

from pathlib import Path

import pytest

from neil_agent.instructions import (
    MAX_INSTRUCTIONS_FILE_BYTES,
    MAX_INSTRUCTIONS_TOTAL_BYTES,
    apply_project_instructions_init,
    load_project_instructions,
    prepare_project_instructions_init,
)
from neil_agent.errors import InstructionError


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


def test_loads_root_to_target_chain_in_outer_to_inner_order(tmp_path: Path) -> None:
    nested = tmp_path / "packages" / "api"
    nested.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("ROOT-RULE", encoding="utf-8")
    (tmp_path / "packages" / "AGENTS.md").write_text(
        "PACKAGE-RULE",
        encoding="utf-8",
    )
    (nested / "AGENTS.md").write_text("API-RULE", encoding="utf-8")

    instructions = load_project_instructions(tmp_path, nested)
    prompt = instructions.prompt_section()

    assert instructions.active
    assert [source.scope for source in instructions.active_sources] == [
        tmp_path,
        tmp_path / "packages",
        nested,
    ]
    assert (
        prompt.index("ROOT-RULE")
        < prompt.index("PACKAGE-RULE")
        < prompt.index("API-RULE")
    )
    assert "outer to inner" in prompt


def test_rejects_target_outside_workspace_and_excessive_cumulative_size(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "one" / "two"
    target.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    for directory in (workspace, workspace / "one", target):
        (directory / "AGENTS.md").write_text("x" * 25_000, encoding="utf-8")

    excessive = load_project_instructions(workspace, target)
    escaped = load_project_instructions(workspace, outside)

    assert MAX_INSTRUCTIONS_TOTAL_BYTES < 75_000
    assert excessive.status == "invalid"
    assert "累计" in excessive.reason
    assert escaped.status == "invalid"
    assert "越过工作区" in escaped.reason


def test_init_builds_local_preview_and_exclusively_creates_root_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "sample-agent"\n[dependency-groups]\n'
        'dev = ["pytest", "ruff", "mypy"]\n',
        encoding="utf-8",
    )

    candidate = prepare_project_instructions_init(tmp_path)
    apply_project_instructions_init(candidate)

    content = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "sample-agent" in content
    assert "pytest -q" in content
    assert "+++ AGENTS.md" in candidate.preview
    with pytest.raises(InstructionError, match="不会覆盖"):
        prepare_project_instructions_init(tmp_path)


def test_init_rechecks_existence_after_preview(tmp_path: Path) -> None:
    candidate = prepare_project_instructions_init(tmp_path)
    (tmp_path / "AGENTS.md").write_text("user-created", encoding="utf-8")

    with pytest.raises(InstructionError, match="批准后已出现"):
        apply_project_instructions_init(candidate)

    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "user-created"
