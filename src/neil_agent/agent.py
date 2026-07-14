"""Conversation orchestration for Neil Agent."""

from __future__ import annotations

from collections.abc import Generator, Iterator, Sequence
from typing import Protocol

from .config import DEFAULT_SYSTEM_PROMPT
from .errors import AgentError
from .schemas import Message, ModelResponse, ToolDefinition
from .tools.registry import ToolRegistry


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
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be at least 1")

        self._llm = llm
        self._system_prompt = system_prompt
        self._max_rounds = max_rounds
        self._registry = registry
        self._max_tool_rounds = max_tool_rounds
        self._messages: list[Message] = []

    @property
    def messages(self) -> tuple[Message, ...]:
        """Return an immutable snapshot of the successful message history."""

        return tuple(self._messages)

    def clear(self) -> None:
        """Start a new conversation."""

        self._messages.clear()

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
            model_response: ModelResponse | None = None
            for event in self._llm.stream(
                request_messages,
                system_prompt=self._system_prompt,
                tools=tool_definitions,
            ):
                if isinstance(event, str):
                    yield event
                else:
                    model_response = event

            if model_response is None:
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
                    self._registry.execute(call) for call in model_response.tool_calls
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
