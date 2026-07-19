"""Safe, bounded formatting for user-visible execution activity."""

from __future__ import annotations

from dataclasses import dataclass
from unicodedata import category

from .schemas import ToolCall, ToolResult

WRITE_TOOL_NAMES = frozenset({"write_file", "replace_text"})
COMMAND_TOOL_NAMES = frozenset(
    {
        "run_quality_check",
        "git_status",
        "git_diff",
        "git_stage",
        "git_commit",
    }
)


@dataclass(frozen=True, slots=True)
class ToolActivity:
    """A safe display title and bounded input details for one tool call."""

    title: str
    details: tuple[str, ...] = ()


def describe_tool_call(call: ToolCall) -> ToolActivity:
    """Describe one tool call with bounded, user-useful input details."""

    path = _safe_text(call.arguments.get("path"), "未指定路径")
    if call.name == "list_directory":
        return ToolActivity("查看目录", (f"路径：{path}",))
    if call.name == "read_file":
        return ToolActivity("读取文件", (f"路径：{path}",))
    if call.name == "search_text":
        query = _safe_text(call.arguments.get("query"), "未指定")
        return ToolActivity("搜索文本", (f"范围：{path}", f"查询：{query}"))
    if call.name == "write_file":
        return ToolActivity(
            "写入文件",
            (
                f"路径：{path}",
                f"内容规模：{_text_metrics(call.arguments.get('content'))}",
            ),
        )
    if call.name == "replace_text":
        replacements = _safe_text(
            call.arguments.get("expected_replacements"),
            "1",
        )
        return ToolActivity(
            "替换文本",
            (
                f"路径：{path}",
                f"预计替换：{replacements} 处",
                "替换规模："
                f"{_text_metrics(call.arguments.get('old_text'))} → "
                f"{_text_metrics(call.arguments.get('new_text'))}",
            ),
        )
    if call.name == "run_quality_check":
        check = _safe_text(call.arguments.get("check"), "未指定")
        return ToolActivity("运行质量检查", (f"检查：{check}",))
    if call.name == "git_status":
        return ToolActivity("检查 Git 状态")
    if call.name == "git_diff":
        scope = "暂存区" if call.arguments.get("staged") is True else "工作区"
        return ToolActivity("查看 Git 差异", (f"范围：{scope}",))
    if call.name == "git_stage":
        paths = call.arguments.get("paths")
        return ToolActivity(
            "暂存文件",
            (
                f"文件数量：{_path_count(paths)}",
                f"路径：{_path_summary(paths)}",
            ),
        )
    if call.name == "git_commit":
        message = _safe_text(call.arguments.get("message"), "未指定")
        return ToolActivity("创建本地 Git 提交", (f"消息：{message}",))
    if call.name == "set_task_plan":
        steps = call.arguments.get("steps")
        count = len(steps) if isinstance(steps, list) else 0
        return ToolActivity("创建任务计划", (f"步骤数量：{count}",))
    if call.name == "update_task_step":
        step_number = _safe_text(call.arguments.get("step_number"))
        status = _safe_text(call.arguments.get("status"))
        return ToolActivity(
            "更新任务步骤",
            (f"步骤：{step_number}", f"状态：{status}"),
        )

    tool_name = _safe_text(call.name, "未知工具")
    argument_names = ", ".join(sorted(call.arguments)) or "无"
    return ToolActivity(
        f"执行工具：{tool_name}",
        (f"参数字段：{_safe_text(argument_names)}",),
    )


def describe_tool_result(call: ToolCall, result: ToolResult) -> tuple[str, ...]:
    """Summarize a result without dumping large file or command output."""

    if call.name in COMMAND_TOOL_NAMES:
        return _command_result_details(call.name, result.content)
    if result.is_error:
        return (f"错误：{_result_excerpt(result.content)}",)
    if call.name == "list_directory":
        return _directory_result_details(result.content)
    if call.name == "read_file":
        return (f"结果：{_text_metrics(result.content)}",)
    if call.name == "search_text":
        return _search_result_details(result.content)
    if call.name in WRITE_TOOL_NAMES:
        return (f"结果：{_result_excerpt(result.content)}",)
    if call.name in {"set_task_plan", "update_task_step"}:
        return ()
    return (f"结果：{_result_excerpt(result.content)}",)


def safe_tool_name(value: object) -> str:
    """Return a terminal-safe, bounded tool name."""

    return _safe_text(value, "未知工具")


def _directory_result_details(content: str) -> tuple[str, ...]:
    if content == "目录为空。":
        return ("结果：目录为空",)
    entries = content.splitlines()
    return (
        f"结果：{len(entries)} 个条目",
        *(f"条目：{_safe_text(entry, max_chars=160)}" for entry in entries[:3]),
        *((f"其余：{len(entries) - 3} 个条目",) if len(entries) > 3 else ()),
    )


def _search_result_details(content: str) -> tuple[str, ...]:
    if content == "未找到匹配内容。":
        return ("结果：未找到匹配内容",)
    result_limit_message = "结果已限制为前 100 条。"
    match_lines = [
        line for line in content.splitlines() if line != result_limit_message
    ]
    suffix = "（已达到上限）" if result_limit_message in content else ""
    locations = [line.partition(": ")[0] for line in match_lines]
    return (
        f"结果：{len(match_lines)} 条匹配{suffix}",
        *(f"位置：{_safe_text(location, max_chars=160)}" for location in locations[:3]),
        *((f"其余：{len(match_lines) - 3} 条匹配",) if len(match_lines) > 3 else ()),
    )


def _command_result_details(tool_name: str, content: str) -> tuple[str, ...]:
    command = _field_value(content, "Command")
    exit_code = _field_value(content, "Exit code")
    output = content.split("Output:\n", maxsplit=1)[-1]
    details: list[str] = []
    if command is not None:
        details.append(f"命令：{_safe_text(command, max_chars=180)}")
    if exit_code is not None:
        details.append(f"退出码：{_safe_text(exit_code)}")
    output_lines = [line.strip() for line in output.splitlines() if line.strip()]
    if tool_name == "git_diff":
        details.append(f"差异输出：{len(output_lines)} 行，{len(output)} 字符")
        changed_files = _diff_file_names(output_lines)
        if changed_files:
            details.append(f"文件：{', '.join(changed_files)}")
    elif tool_name == "git_status":
        if not output_lines or output_lines == ["(no output)"]:
            details.append("结果摘要：工作区干净")
        else:
            details.extend(
                f"状态：{_safe_text(line, max_chars=180)}" for line in output_lines[:4]
            )
            if len(output_lines) > 4:
                details.append(f"其余：{len(output_lines) - 4} 行状态")
    elif output_lines:
        details.append(f"结果摘要：{_safe_text(output_lines[-1], max_chars=180)}")
    else:
        details.append("结果摘要：无输出")
    return tuple(details)


def _diff_file_names(lines: list[str]) -> tuple[str, ...]:
    names: list[str] = []
    for line in lines:
        if not line.startswith("diff --git "):
            continue
        candidate = line.rsplit(" b/", maxsplit=1)[-1]
        safe_name = _safe_text(candidate, max_chars=60)
        if safe_name not in names:
            names.append(safe_name)
        if len(names) == 4:
            break
    return tuple(names)


def _field_value(content: str, field: str) -> str | None:
    prefix = f"{field}: "
    for line in content.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix)
    return None


def _result_excerpt(content: str) -> str:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return _safe_text(lines[-1] if lines else "无输出", max_chars=180)


def _text_metrics(value: object) -> str:
    if not isinstance(value, str):
        return "未知"
    line_count = len(value.splitlines())
    if value and line_count == 0:
        line_count = 1
    return f"{line_count} 行，{len(value)} 字符"


def _safe_text(
    value: object,
    fallback: str = "未知",
    *,
    max_chars: int = 80,
) -> str:
    if value is None:
        return fallback
    safe_value = "".join(
        " " if category(character).startswith("C") else character
        for character in str(value)
    )
    text = " ".join(safe_value.split())
    if not text:
        return fallback
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."


def _path_count(value: object) -> int:
    if isinstance(value, list):
        return len(value)
    return 0


def _path_summary(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "未指定"
    visible = [_safe_text(path, max_chars=60) for path in value[:3]]
    summary = ", ".join(visible)
    if len(value) > 3:
        summary += f"，另有 {len(value) - 3} 个"
    return summary
