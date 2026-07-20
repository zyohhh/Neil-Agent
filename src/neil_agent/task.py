"""In-memory task planning and verification status for one CLI session."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .errors import ToolError
from .schemas import ToolCall, ToolDefinition, ToolResult
from .tools.registry import ToolRegistry

PlanStepStatus = Literal["pending", "in_progress", "completed"]
QualityCheckStatus = Literal["passed", "failed", "not_run"]
PlanChangeHandler = Callable[[str], None]

MAX_TASK_STEPS = 5
MAX_TASK_STEP_CHARS = 200
MAX_QUALITY_OUTPUT_CHARS = 500


@dataclass(frozen=True, slots=True)
class TaskStep:
    """One immutable task-plan step."""

    title: str
    status: PlanStepStatus


@dataclass(frozen=True, slots=True)
class QualityCheckRecord:
    """The most recent quality-check attempt shown by ``/status``."""

    check: str
    status: QualityCheckStatus
    command: str | None
    exit_code: int | None
    output: str


class TaskTracker:
    """Track one visible plan and the latest quality-check result in memory."""

    def __init__(self, change_handler: PlanChangeHandler | None = None) -> None:
        self._steps: tuple[TaskStep, ...] = ()
        self._latest_quality_check: QualityCheckRecord | None = None
        self._change_handler = change_handler

    @property
    def steps(self) -> tuple[TaskStep, ...]:
        """Return the current immutable plan snapshot."""

        return self._steps

    @property
    def latest_quality_check(self) -> QualityCheckRecord | None:
        """Return the latest quality-check attempt, if one exists."""

        return self._latest_quality_check

    def register(self, registry: ToolRegistry) -> None:
        """Register model-facing plan creation and update tools."""

        registry.register(SET_TASK_PLAN, self.set_task_plan)
        registry.register(UPDATE_TASK_STEP, self.update_task_step)

    def clear(self) -> None:
        """Clear task-local state when the conversation is reset."""

        self._steps = ()
        self._latest_quality_check = None

    def restore(
        self,
        steps: tuple[TaskStep, ...],
        latest_quality_check: QualityCheckRecord | None,
    ) -> None:
        """Replace task state after validating a persisted snapshot."""

        if len(steps) > MAX_TASK_STEPS:
            raise ValueError(f"restored plan exceeds {MAX_TASK_STEPS} steps")
        if steps:
            try:
                titles = self._validate_steps([step.title for step in steps])
            except ToolError as error:
                raise ValueError(str(error)) from error
            if titles != tuple(step.title for step in steps):
                raise ValueError("restored plan titles are not normalized")
            self._validate_restored_statuses(steps)

        self._steps = tuple(steps)
        self._latest_quality_check = latest_quality_check

    def set_task_plan(self, steps: list[str]) -> str:
        """Replace the current plan and start its first step."""

        titles = self._validate_steps(steps)
        self._steps = tuple(
            TaskStep(
                title=title,
                status="in_progress" if index == 0 else "pending",
            )
            for index, title in enumerate(titles)
        )
        return self._plan_changed("任务计划已创建。")

    def update_task_step(self, step_number: int, status: str) -> str:
        """Start or complete one step while preserving a valid plan order."""

        if not self._steps:
            raise ToolError("当前没有任务计划，请先调用 set_task_plan。")
        if not isinstance(step_number, int) or isinstance(step_number, bool):
            raise ToolError("step_number 必须是整数。")
        if step_number < 1 or step_number > len(self._steps):
            raise ToolError(f"step_number 必须在 1 到 {len(self._steps)} 之间。")
        if not isinstance(status, str) or status not in {"in_progress", "completed"}:
            raise ToolError("status 只能是 in_progress 或 completed。")

        index = step_number - 1
        current = self._steps[index]
        if current.status == status:
            return self.format_plan()
        if current.status == "completed":
            raise ToolError("已完成的步骤不能重新打开。")

        updated = list(self._steps)
        if status == "in_progress":
            if current.status != "pending":
                raise ToolError("只有待处理步骤可以设为进行中。")
            if any(step.status == "in_progress" for step in self._steps):
                raise ToolError("请先完成当前进行中的步骤。")
            if any(step.status != "completed" for step in self._steps[:index]):
                raise ToolError("必须按顺序推进任务计划。")
            updated[index] = TaskStep(current.title, "in_progress")
        else:
            if current.status != "in_progress":
                raise ToolError("只有进行中的步骤可以标记为完成。")
            updated[index] = TaskStep(current.title, "completed")
            next_index = index + 1
            if next_index < len(updated) and updated[next_index].status == "pending":
                next_step = updated[next_index]
                updated[next_index] = TaskStep(next_step.title, "in_progress")

        self._steps = tuple(updated)
        return self._plan_changed(f"任务步骤 {step_number} 已更新。")

    def record_tool_result(self, call: ToolCall, result: ToolResult) -> None:
        """Capture the latest quality-check attempt from Agent tool execution."""

        if call.name != "run_quality_check":
            return
        check = call.arguments.get("check")
        check_name = check if isinstance(check, str) else "unknown"
        command = self._field_value(result.content, "Command")
        exit_code_text = self._field_value(result.content, "Exit code")
        try:
            exit_code = int(exit_code_text) if exit_code_text is not None else None
        except ValueError:
            exit_code = None

        if not result.is_error:
            status: QualityCheckStatus = "passed"
        elif any(
            marker in result.content
            for marker in ("用户拒绝", "无法请求确认", "需要用户确认")
        ):
            status = "not_run"
        else:
            status = "failed"
        output = self._output_value(result.content)
        self._latest_quality_check = QualityCheckRecord(
            check=check_name,
            status=status,
            command=command,
            exit_code=exit_code,
            output=self._truncate_quality_output(output),
        )

    def format_plan(self) -> str:
        """Format the current plan for CLI display and tool results."""

        lines = ["当前任务计划"]
        if not self._steps:
            lines.append("  （尚未创建）")
            return "\n".join(lines)
        markers = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines.extend(
            f"  {markers[step.status]} {index}. {step.title} [{step.status}]"
            for index, step in enumerate(self._steps, start=1)
        )
        return "\n".join(lines)

    def format_status(self, git_status: str) -> str:
        """Format plan, latest verification, and current Git state."""

        quality_lines = ["最近质量检查"]
        record = self._latest_quality_check
        if record is None:
            quality_lines.append("  （尚未运行）")
        else:
            quality_lines.append(f"  {record.check}: {record.status}")
            if record.command is not None:
                quality_lines.append(f"  Command: {record.command}")
            if record.exit_code is not None:
                quality_lines.append(f"  Exit code: {record.exit_code}")
            quality_lines.append("  Output:")
            quality_lines.extend(
                f"    {line}" for line in record.output.splitlines() or ["(no output)"]
            )

        git_lines = ["本地 Git 状态"]
        git_lines.extend(f"  {line}" for line in git_status.splitlines())
        return "\n\n".join(
            [
                self.format_plan(),
                "\n".join(quality_lines),
                "\n".join(git_lines),
            ]
        )

    def _plan_changed(self, message: str) -> str:
        formatted = self.format_plan()
        if self._change_handler is not None:
            self._change_handler(formatted)
        return f"{message}\n{formatted}"

    @staticmethod
    def _validate_steps(steps: list[str]) -> tuple[str, ...]:
        if not isinstance(steps, list) or not steps:
            raise ToolError("steps 必须是包含 1 到 5 项的字符串列表。")
        if len(steps) > MAX_TASK_STEPS:
            raise ToolError(f"任务计划最多包含 {MAX_TASK_STEPS} 个步骤。")
        titles: list[str] = []
        for step in steps:
            if not isinstance(step, str) or not step.strip():
                raise ToolError("每个任务步骤都必须是非空字符串。")
            title = step.strip()
            if len(title) > MAX_TASK_STEP_CHARS:
                raise ToolError(f"每个任务步骤不能超过 {MAX_TASK_STEP_CHARS} 个字符。")
            if title in titles:
                raise ToolError("任务计划不能包含重复步骤。")
            titles.append(title)
        return tuple(titles)

    @staticmethod
    def _validate_restored_statuses(steps: tuple[TaskStep, ...]) -> None:
        in_progress = [
            index for index, step in enumerate(steps) if step.status == "in_progress"
        ]
        if len(in_progress) > 1:
            raise ValueError("restored plan has multiple in-progress steps")
        if not in_progress:
            if any(step.status != "completed" for step in steps):
                raise ValueError("unfinished restored plan needs an in-progress step")
            return

        active_index = in_progress[0]
        if any(step.status != "completed" for step in steps[:active_index]):
            raise ValueError("steps before the active step must be completed")
        if any(step.status != "pending" for step in steps[active_index + 1 :]):
            raise ValueError("steps after the active step must be pending")

    @staticmethod
    def _field_value(content: str, field: str) -> str | None:
        prefix = f"{field}: "
        for line in content.splitlines():
            if line.startswith(prefix):
                return line.removeprefix(prefix)
        return None

    @staticmethod
    def _output_value(content: str) -> str:
        marker = "Output:\n"
        if marker in content:
            return content.split(marker, maxsplit=1)[1]
        return content

    @staticmethod
    def _truncate_quality_output(output: str) -> str:
        if len(output) <= MAX_QUALITY_OUTPUT_CHARS:
            return output
        half = MAX_QUALITY_OUTPUT_CHARS // 2
        return output[:half] + "\n... status output truncated ...\n" + output[-half:]


SET_TASK_PLAN = ToolDefinition(
    name="set_task_plan",
    description=(
        "Create or replace the visible plan for one multi-step development task. "
        "Use 1 to 5 concise steps; the first starts in progress and the rest are "
        "pending. This only changes in-memory task status."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_TASK_STEP_CHARS,
                },
                "minItems": 1,
                "maxItems": MAX_TASK_STEPS,
                "description": "Ordered, concise development task steps.",
            }
        },
        "required": ["steps"],
        "additionalProperties": False,
    },
)

UPDATE_TASK_STEP = ToolDefinition(
    name="update_task_step",
    description=(
        "Advance one visible task-plan step to in_progress or completed. Complete "
        "steps in order; completing a step automatically starts the next one."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "step_number": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_TASK_STEPS,
                "description": "One-based task step number.",
            },
            "status": {
                "type": "string",
                "enum": ["in_progress", "completed"],
                "description": "New task step status.",
            },
        },
        "required": ["step_number", "status"],
        "additionalProperties": False,
    },
)
