"""Conversation orchestration for Neil Agent."""

from __future__ import annotations

from collections.abc import Generator, Iterator, Sequence
from typing import Protocol

from .config import DEFAULT_SYSTEM_PROMPT
from .llm import LLMError
from .schemas import Message


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
    ) -> Iterator[str]: ...


class Agent:
    """Manage successful user/assistant rounds and call the chat model."""

    def __init__(
        self,
        llm: ChatModel,
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_rounds: int = 20,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")

        self._llm = llm
        self._system_prompt = system_prompt
        self._max_rounds = max_rounds
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
        self._commit_round(user_message, assistant_message)
        return response

    def stream_chat(self, user_input: str) -> Generator[str, None, None]:
        """Yield one response as it arrives, then save the completed round."""

        user_message = self._make_user_message(user_input)
        request_messages = self._request_messages(user_message)
        chunks: list[str] = []

        for chunk in self._llm.stream(
            request_messages,
            system_prompt=self._system_prompt,
        ):
            chunks.append(chunk)
            yield chunk

        assistant_message = self._make_assistant_message("".join(chunks))
        self._commit_round(user_message, assistant_message)

    @staticmethod
    def _make_user_message(user_input: str) -> Message:
        content = user_input.strip()
        if not content:
            raise ValueError("用户输入不能为空。")
        return Message(role="user", content=content)

    @staticmethod
    def _make_assistant_message(response: str) -> Message:
        if not response.strip():
            raise LLMError("模型返回了空内容，请重新尝试。")
        return Message(role="assistant", content=response)

    def _request_messages(self, user_message: Message) -> list[Message]:
        previous_message_limit = (self._max_rounds - 1) * 2
        if previous_message_limit == 0:
            return [user_message]
        return [*self._messages[-previous_message_limit:], user_message]

    def _commit_round(self, user_message: Message, assistant_message: Message) -> None:
        self._messages.extend((user_message, assistant_message))
        message_limit = self._max_rounds * 2
        if len(self._messages) > message_limit:
            del self._messages[:-message_limit]
