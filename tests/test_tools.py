"""Tests for read-only filesystem tools and registry dispatch."""

from pathlib import Path

import pytest

from neil_agent.schemas import ToolCall, ToolDefinition
from neil_agent.tools.filesystem import FileSystemTools
from neil_agent.tools.registry import ToolRegistry


def test_filesystem_tools_list_read_and_search(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("Neil Agent\n", encoding="utf-8")
    (tmp_path / "src" / "main.py").write_text(
        "print('Needle')\n",
        encoding="utf-8",
    )
    tools = FileSystemTools(tmp_path)

    listing = tools.list_directory()
    content = tools.read_file("README.md")
    matches = tools.search_text("needle", "src")

    assert "FILE README.md" in listing
    assert "DIR  src/" in listing
    assert content == "Neil Agent\n"
    assert "src/main.py:1" in matches


def test_registry_dispatches_tool_call(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    registry = ToolRegistry()
    FileSystemTools(tmp_path).register(registry)

    result = registry.execute(
        ToolCall(
            id="call-1",
            name="read_file",
            arguments={"path": "README.md"},
        )
    )

    assert result.content == "hello"
    assert result.is_error is False
    assert [definition.name for definition in registry.definitions] == [
        "list_directory",
        "read_file",
        "search_text",
    ]


def test_registry_returns_errors_for_unknown_tool_and_bad_arguments() -> None:
    registry = ToolRegistry()
    unknown = registry.execute(ToolCall(id="call-1", name="missing"))
    registry.register(
        ToolDefinition(
            name="echo",
            description="Echo text.",
            input_schema={"type": "object"},
        ),
        lambda text: text,
    )
    bad_arguments = registry.execute(ToolCall(id="call-2", name="echo"))

    assert unknown.is_error is True
    assert "未知工具" in unknown.content
    assert bad_arguments.is_error is True
    assert "参数错误" in bad_arguments.content


def test_registry_rejects_duplicate_names() -> None:
    definition = ToolDefinition(
        name="echo",
        description="Echo text.",
        input_schema={"type": "object"},
    )
    registry = ToolRegistry()
    registry.register(definition, lambda: "first")

    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition, lambda: "second")
