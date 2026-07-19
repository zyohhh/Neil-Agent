"""Tests for bounded, tool-specific execution activity formatting."""

from neil_agent.activity import describe_tool_call, describe_tool_result
from neil_agent.schemas import ToolCall, ToolResult


def test_write_activity_shows_scale_without_copying_content() -> None:
    call = ToolCall(
        id="call-write",
        name="write_file",
        arguments={
            "path": "notes\x1b[31m.txt\nnext",
            "content": "TOP-SECRET\nsecond line",
        },
    )

    activity = describe_tool_call(call)
    activity_text = " ".join((activity.title, *activity.details))

    assert activity.title == "写入文件"
    assert "内容规模：2 行，22 字符" in activity.details
    assert "TOP-SECRET" not in activity_text
    assert "\x1b" not in activity_text
    assert "\n" not in activity_text


def test_directory_and_search_results_include_bounded_locations() -> None:
    directory_call = ToolCall(id="call-list", name="list_directory", arguments={})
    directory_result = ToolResult(
        tool_call_id=directory_call.id,
        content="FILE a.py\nFILE b.py\nDIR src/\nFILE README.md",
    )
    search_call = ToolCall(id="call-search", name="search_text", arguments={})
    search_result = ToolResult(
        tool_call_id=search_call.id,
        content="a.py:1: match\nb.py:2: match\nsrc/c.py:3: match\nREADME.md:4: match",
    )

    directory_details = describe_tool_result(directory_call, directory_result)
    search_details = describe_tool_result(search_call, search_result)

    assert directory_details[-1] == "其余：1 个条目"
    assert "条目：FILE a.py" in directory_details
    assert search_details[-1] == "其余：1 条匹配"
    assert "位置：a.py:1" in search_details
    assert all("match" not in detail for detail in search_details)


def test_failed_command_result_keeps_command_exit_code_and_summary() -> None:
    call = ToolCall(id="call-check", name="run_quality_check", arguments={})
    result = ToolResult(
        tool_call_id=call.id,
        content=(
            "Command: python -m pytest -q\n"
            "Working directory: D:/project\n"
            "Exit code: 1\n"
            "Output:\n1 failed, 64 passed"
        ),
        is_error=True,
    )

    details = describe_tool_result(call, result)

    assert details == (
        "命令：python -m pytest -q",
        "退出码：1",
        "结果摘要：1 failed, 64 passed",
    )
