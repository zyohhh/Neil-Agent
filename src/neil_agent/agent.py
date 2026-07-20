"""Conversation orchestration for Neil Agent."""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator, Sequence
from time import monotonic
from typing import Protocol

from .activity import (
    ToolActivity,
    describe_tool_call,
    describe_tool_result,
    safe_tool_name,
)
from .config import DEFAULT_SYSTEM_PROMPT
from .errors import AgentError
from .schemas import (
    ActivityEvent,
    ActivityStatus,
    Message,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    ToolResult,
    validate_message_history,
)
from .task import TaskTracker
from .tools.registry import ToolRegistry

ToolApprovalHandler = Callable[[ToolCall, str], bool]
ActivityHandler = Callable[[ActivityEvent], None]

TOOL_WORKFLOW_INSTRUCTIONS = """Local tool workflow requirements:
- After a successful write_file or replace_text call, choose an appropriate
  run_quality_check for the changed code. Do not run every check without reason.
- In the final answer, summarize each attempted check using its exact Command,
  Exit code, and the key Output. If approval was denied, say that it was not run.
- Before creating a local commit, inspect Git changes, stage only explicit paths,
  and never claim that a commit was pushed unless a separate push actually occurred."""

TASK_PLAN_INSTRUCTIONS = """Visible task-plan requirements:
- For a development request that needs multiple actions, call set_task_plan with
  no more than five concise steps before making changes.
- Keep the plan accurate with update_task_step. Complete steps in order and do
  not mark work completed before it is actually finished.
- Combine plan updates with related tool calls when possible so plan tracking
  does not consume unnecessary tool rounds."""

WRITE_TOOL_NAMES = frozenset({"write_file", "replace_text"})


class ChatModel(Protocol):
    """The model operations required by the conversation agent."""

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str: ...

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]: ...


class Agent:
    """Manage successful user/assistant rounds and call the chat model."""

    def __init__(
        self,
        llm: ChatModel,
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_rounds: int = 20,
        registry: ToolRegistry | None = None,
        max_tool_rounds: int = 5,
        approval_handler: ToolApprovalHandler | None = None,
        task_tracker: TaskTracker | None = None,
        activity_handler: ActivityHandler | None = None,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be at least 1")

        self._llm = llm
        self._system_prompt = self._with_tool_workflow(system_prompt, registry)
        self._max_rounds = max_rounds
        self._registry = registry
        self._max_tool_rounds = max_tool_rounds
        self._approval_handler = approval_handler
        self._task_tracker = task_tracker
        self._activity_handler = activity_handler
        self._messages: list[Message] = []

    @property
    def messages(self) -> tuple[Message, ...]:
        """Return an immutable snapshot of the successful message history."""

        return tuple(self._messages)

    def clear(self) -> None:
        """Start a new conversation."""

        self._messages.clear()
        if self._task_tracker is not None:
            self._task_tracker.clear()

    def restore_messages(self, messages: Sequence[Message]) -> None:
        """Replace history with validated, complete persisted rounds."""

        validate_message_history(messages)
        restored = list(messages)
        round_starts = [
            index
            for index, message in enumerate(restored)
            if message.role == "user" and not message.tool_results
        ]
        if len(round_starts) > self._max_rounds:
            restored = restored[round_starts[-self._max_rounds] :]
        self._messages = restored

    def chat(self, user_input: str) -> str:
        """Send one user message and return the complete assistant response."""

        user_message = self._make_user_message(user_input)
        request_messages = self._request_messages(user_message)
        response = self._llm.complete(
            request_messages,
            system_prompt=self._system_prompt,
        )
        assistant_message = self._make_assistant_message(response)
        self._commit_messages((user_message, assistant_message))
        return response

    def stream_chat(self, user_input: str) -> Generator[str, None, None]:
        """Yield one response as it arrives, then save the completed round."""

        user_message = self._make_user_message(user_input)
        request_messages = self._request_messages(user_message)
        pending_messages = [user_message]
        tool_definitions = self._tool_definitions()
        tool_rounds = 0

        while True:
            model_activity = (
                "分析用户请求" if tool_rounds == 0 else "根据工具结果继续处理"
            )
            self._emit_activity(
                "running",
                model_activity,
                (
                    f"模型轮次：{tool_rounds + 1}",
                    f"上下文消息：{len(request_messages)} 条",
                    f"可用工具：{len(tool_definitions)} 个",
                ),
            )
            model_response: ModelResponse | None = None
            try:
                for event in self._llm.stream(
                    request_messages,
                    system_prompt=self._system_prompt,
                    tools=tool_definitions,
                ):
                    if isinstance(event, str):
                        yield event
                    else:
                        model_response = event
            except Exception:
                self._emit_activity("failed", "模型请求失败")
                raise

            if model_response is None:
                self._emit_activity("failed", "模型响应不完整")
                raise AgentError("模型流式响应缺少结束事件，请重新尝试。")

            assistant_message = Message(
                role="assistant",
                content=model_response.content,
                thinking=model_response.thinking,
                tool_calls=model_response.tool_calls,
            )
            request_messages.append(assistant_message)
            pending_messages.append(assistant_message)

            if not model_response.tool_calls:
                self._commit_messages(pending_messages)
                return

            self._emit_activity(
                "succeeded",
                f"模型请求 {len(model_response.tool_calls)} 个工具",
                tuple(
                    f"{index}. {safe_tool_name(call.name)}"
                    for index, call in enumerate(model_response.tool_calls, start=1)
                ),
            )

            if self._registry is None:
                raise AgentError("模型请求了工具，但当前没有可用的工具注册表。")

            tool_rounds += 1
            if tool_rounds > self._max_tool_rounds:
                raise AgentError(
                    f"工具调用超过 {self._max_tool_rounds} 轮，已停止本次任务。"
                )

            tool_result_message = Message(
                role="user",
                tool_results=tuple(
                    self._execute_tool_call(call) for call in model_response.tool_calls
                ),
            )
            request_messages.append(tool_result_message)
            pending_messages.append(tool_result_message)

    @staticmethod
    def _make_user_message(user_input: str) -> Message:
        content = user_input.strip()
        if not content:
            raise ValueError("用户输入不能为空。")
        return Message(role="user", content=content)

    @staticmethod
    def _make_assistant_message(response: str) -> Message:
        if not response.strip():
            raise AgentError("模型返回了空内容，请重新尝试。")
        return Message(role="assistant", content=response)

    def _request_messages(self, user_message: Message) -> list[Message]:
        previous_round_limit = self._max_rounds - 1
        if previous_round_limit == 0:
            return [user_message]
        round_starts = self._conversation_round_starts()
        if len(round_starts) > previous_round_limit:
            history = self._messages[round_starts[-previous_round_limit] :]
        else:
            history = self._messages
        return [*history, user_message]

    def _commit_messages(self, messages: Sequence[Message]) -> None:
        self._messages.extend(messages)
        round_starts = self._conversation_round_starts()
        if len(round_starts) > self._max_rounds:
            del self._messages[: round_starts[-self._max_rounds]]

    def _conversation_round_starts(self) -> list[int]:
        return [
            index
            for index, message in enumerate(self._messages)
            if message.role == "user" and not message.tool_results
        ]

    def _tool_definitions(self) -> tuple[ToolDefinition, ...]:
        if self._registry is None:
            return ()
        return self._registry.definitions

    @staticmethod
    def _with_tool_workflow(
        system_prompt: str,
        registry: ToolRegistry | None,
    ) -> str:
        """Append non-configurable workflow rules when matching tools exist."""

        if registry is None:
            return system_prompt
        tool_names = {definition.name for definition in registry.definitions}
        instructions: list[str] = []
        if (
            WRITE_TOOL_NAMES.intersection(tool_names)
            and "run_quality_check" in tool_names
        ):
            instructions.append(TOOL_WORKFLOW_INSTRUCTIONS)
        if {"set_task_plan", "update_task_step"}.issubset(tool_names):
            instructions.append(TASK_PLAN_INSTRUCTIONS)
        if not instructions:
            return system_prompt
        return f"{system_prompt.rstrip()}\n\n" + "\n\n".join(instructions)

    def _execute_tool_call(self, call: ToolCall) -> ToolResult:
        started_at = monotonic()
        activity = describe_tool_call(call)
        self._emit_activity("running", activity.title, activity.details)

        if self._registry is None:
            result = ToolResult(
                tool_call_id=call.id,
                content="当前没有可用的工具注册表。",
                is_error=True,
            )
            return self._finish_tool_call(
                call,
                result,
                activity,
                started_at,
            )
        if not self._registry.requires_approval(call.name):
            result = self._registry.execute(call)
            return self._finish_tool_call(
                call,
                result,
                activity,
                started_at,
            )

        preview = self._registry.preview(call)
        if preview.is_error:
            return self._finish_tool_call(
                call,
                preview,
                activity,
                started_at,
            )
        if self._approval_handler is None:
            result = ToolResult(
                tool_call_id=call.id,
                content=f"工具需要用户确认，但当前无法请求确认：{call.name}",
                is_error=True,
            )
            return self._finish_tool_call(
                call,
                result,
                activity,
                started_at,
            )
        self._emit_activity(
            "waiting",
            f"等待批准：{activity.title}",
            (*activity.details, "已生成操作预览，确认后才会执行"),
        )
        if not self._approval_handler(call, preview.content):
            result = ToolResult(
                tool_call_id=call.id,
                content=f"用户拒绝执行工具：{call.name}",
                is_error=True,
            )
            return self._finish_tool_call(
                call,
                result,
                activity,
                started_at,
                skipped=True,
            )
        self._emit_activity(
            "running",
            f"执行：{activity.title}",
            activity.details,
        )
        result = self._registry.execute(
            call,
            approved=True,
            approved_preview=preview.content,
        )
        return self._finish_tool_call(
            call,
            result,
            activity,
            started_at,
        )

    def _finish_tool_call(
        self,
        call: ToolCall,
        result: ToolResult,
        activity: ToolActivity,
        started_at: float,
        *,
        skipped: bool = False,
    ) -> ToolResult:
        """Record observable task state and append safe workflow guidance."""

        if self._task_tracker is not None:
            self._task_tracker.record_tool_result(call, result)
        elapsed = monotonic() - started_at
        if skipped:
            self._emit_activity(
                "skipped",
                activity.title,
                (*activity.details, "结果：用户拒绝，未执行"),
            )
        elif result.is_error:
            self._emit_activity(
                "failed",
                activity.title,
                (
                    *activity.details,
                    *describe_tool_result(call, result),
                    f"耗时：{elapsed:.1f}s",
                ),
            )
        else:
            self._emit_activity(
                "succeeded",
                activity.title,
                (
                    *activity.details,
                    *describe_tool_result(call, result),
                    f"耗时：{elapsed:.1f}s",
                ),
            )
        return self._with_post_tool_guidance(call, result)

    def _emit_activity(
        self,
        status: ActivityStatus,
        message: str,
        details: tuple[str, ...] = (),
    ) -> None:
        """Publish a high-level activity without exposing model reasoning."""

        if self._activity_handler is None:
            return
        self._activity_handler(
            ActivityEvent(status=status, message=message, details=details)
        )

    @staticmethod
    def _with_post_tool_guidance(call: ToolCall, result: ToolResult) -> ToolResult:
        """Give the model the next safe workflow step after successful mutations."""

        if result.is_error:
            return result
        guidance = ""
        if call.name in WRITE_TOOL_NAMES and "没有变化" not in result.content:
            guidance = (
                "下一步：根据本次修改选择合适的 run_quality_check；"
                "最终回答需汇总命令、退出码和关键结果。"
            )
        elif call.name == "git_stage":
            guidance = (
                "下一步：使用 git_diff(staged=true) 检查暂存内容；"
                "只有用户要求时才调用 git_commit。"
            )
        elif call.name == "git_commit":
            guidance = "本地提交已完成；除非另有工具结果，否则不要声称已经推送。"
        if not guidance:
            return result
        return result.model_copy(update={"content": f"{result.content}\n\n{guidance}"})
