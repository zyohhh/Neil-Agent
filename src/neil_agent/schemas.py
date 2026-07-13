"""Shared data structures used by the agent and its tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MessageRole = Literal["user", "assistant"]


class Message(BaseModel):
    """A single text message in the conversation history."""

    model_config = ConfigDict(frozen=True)

    role: MessageRole
    content: str

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        """Reject empty messages while preserving the original whitespace."""

        if not value.strip():
            raise ValueError("message content must not be blank")
        return value

    def to_api_dict(self) -> dict[str, str]:
        """Convert the message to the shape expected by the model API."""

        return {"role": self.role, "content": self.content}


class ToolCall(BaseModel):
    """A tool invocation requested by the model.

    Tool execution is not part of the first usable version, but defining the
    structure now keeps the future agent-tool boundary explicit.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """The result returned after executing a tool call."""

    model_config = ConfigDict(frozen=True)

    tool_call_id: str = Field(min_length=1)
    content: str
    is_error: bool = False
