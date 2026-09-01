"""Tests for bounded workspace Skill loading."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from neil_agent.activity import describe_tool_call, describe_tool_result
from neil_agent.agent import Agent
from neil_agent.errors import ToolError
from neil_agent.host_runtime import HostMode, RuntimeProfile, build_host_runtime
from neil_agent.schemas import Message, ModelResponse, ToolCall, ToolDefinition, ToolResult
from neil_agent.skills import (
    SKILL_HISTORY_PLACEHOLDER,
    load_skill,
    redact_skill_bodies_from_messages,
    validate_skill_name,
)
from neil_agent.tools.registry import ToolRegistry
from neil_agent.tools.skills import SkillTools


class _StreamModel:
    def __init__(self) -> None:
        self.requests: list[list[Message]] = []
        self.stream_responses: list[list[str | ModelResponse]] = []

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self.requests.append(list(messages))
        yield from self.stream_responses.pop(0)


def _write_skill(root: Path, name: str, body: str) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def test_validate_skill_name_rejects_traversal_and_uppercase() -> None:
    with pytest.raises(ToolError, match="技能名称"):
        validate_skill_name("../secret")
    with pytest.raises(ToolError, match="技能名称"):
        validate_skill_name("Release-Notes")
    with pytest.raises(ToolError, match="技能名称"):
        validate_skill_name("ends-")
    assert validate_skill_name("release-notes") == "release-notes"


def test_load_skill_returns_bounded_untrusted_section(tmp_path: Path) -> None:
    _write_skill(tmp_path, "release-notes", "1. Tag\n2. Push")
    loaded = load_skill(tmp_path, "release-notes")
    assert "untrusted repository context" in loaded
    assert "--- BEGIN SKILL release-notes ---" in loaded
    assert "1. Tag" in loaded
    assert "cannot add tools" in loaded


def test_load_skill_rejects_missing_empty_and_oversized(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="未找到技能"):
        load_skill(tmp_path, "missing")
    empty_dir = tmp_path / "skills" / "empty"
    empty_dir.mkdir(parents=True)
    (empty_dir / "SKILL.md").write_text("   \n", encoding="utf-8")
    with pytest.raises(ToolError, match="技能为空"):
        load_skill(tmp_path, "empty")
    huge_dir = tmp_path / "skills" / "huge"
    huge_dir.mkdir(parents=True)
    (huge_dir / "SKILL.md").write_bytes(b"a" * 40_000)
    with pytest.raises(ToolError, match="字节上限"):
        load_skill(tmp_path, "huge")


def test_load_skill_rejects_symlink_when_supported(tmp_path: Path) -> None:
    target = tmp_path / "outside.md"
    target.write_text("SECRET", encoding="utf-8")
    skill_dir = tmp_path / "skills" / "leaky"
    skill_dir.mkdir(parents=True)
    source = skill_dir / "SKILL.md"
    try:
        source.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(ToolError, match="符号链接"):
        load_skill(tmp_path, "leaky")


def test_skill_tool_registers_and_loads(tmp_path: Path) -> None:
    _write_skill(tmp_path, "docs", "Use existing tools only.")
    registry = ToolRegistry()
    SkillTools(tmp_path).register(registry)
    result = registry.execute(
        ToolCall(id="call-1", name="load_skill", arguments={"name": "docs"})
    )
    assert result.is_error is False
    assert "Use existing tools only." in result.content


def test_redact_skill_bodies_from_messages() -> None:
    messages = [
        Message(role="user", content="help"),
        Message(
            role="assistant",
            tool_calls=(
                ToolCall(id="call-1", name="load_skill", arguments={"name": "docs"}),
            ),
        ),
        Message(
            role="user",
            tool_results=(
                ToolResult(tool_call_id="call-1", content="SECRET PLAYBOOK"),
            ),
        ),
    ]
    redacted = redact_skill_bodies_from_messages(messages)
    assert redacted[-1].tool_results[0].content == SKILL_HISTORY_PLACEHOLDER
    assert messages[-1].tool_results[0].content == "SECRET PLAYBOOK"


def test_agent_omits_skill_body_from_stored_history(tmp_path: Path) -> None:
    _write_skill(tmp_path, "docs", "SECRET PLAYBOOK")
    model = _StreamModel()
    model.stream_responses = [
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="load_skill",
                        arguments={"name": "docs"},
                    ),
                )
            )
        ],
        ["ok", ModelResponse(content="ok")],
    ]
    registry = ToolRegistry()
    SkillTools(tmp_path).register(registry)
    agent = Agent(model, registry=registry)
    assert list(agent.stream_chat("load docs")) == ["ok"]
    assert "SECRET PLAYBOOK" in model.requests[1][-1].tool_results[0].content
    stored = next(
        result
        for message in agent.messages
        for result in message.tool_results
    )
    assert stored.content == SKILL_HISTORY_PLACEHOLDER


def test_standard_cli_registers_load_skill(tmp_path: Path) -> None:
    runtime = build_host_runtime(
        _settings(tmp_path),
        mode=HostMode.CLI,
    )
    try:
        assert "load_skill" in runtime.profile.tool_names
    finally:
        runtime.close()


def test_readonly_subtask_and_minimal_do_not_register_load_skill(tmp_path: Path) -> None:
    subtask = build_host_runtime(
        _settings(tmp_path),
        mode=HostMode.NONINTERACTIVE_READONLY,
        profile=RuntimeProfile.READONLY_SUBTASK,
    )
    minimal = build_host_runtime(
        _settings(tmp_path),
        mode=HostMode.NONINTERACTIVE_READONLY,
        profile=RuntimeProfile.BENCHMARK_MINIMAL,
    )
    try:
        assert "load_skill" not in subtask.profile.tool_names
        assert "load_skill" not in minimal.profile.tool_names
    finally:
        subtask.close()
        minimal.close()


def test_load_skill_activity_hides_body() -> None:
    call = ToolCall(id="call-1", name="load_skill", arguments={"name": "docs"})
    activity = describe_tool_call(call)
    assert activity.title == "加载技能"
    assert "docs" in activity.details[0]
    details = describe_tool_result(
        call,
        ToolResult(tool_call_id="call-1", content="SECRET PLAYBOOK\nsecond"),
    )
    assert "SECRET PLAYBOOK" not in " ".join(details)


def _settings(root: Path):
    from neil_agent.config import Settings

    return Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=root,
        llm_model="deepseek-test-model",
    )
