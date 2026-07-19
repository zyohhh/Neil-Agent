"""Shared data structures used by the agent and its tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MessageRole = Literal["user", "assistant"]
ActivityStatus = Literal["running", "waiting", "succeeded", "skipped", "failed"]


class ActivityEvent(BaseModel):
    """One safe, user-visible update about the agent's current activity."""

    model_config = ConfigDict(frozen=True)

    status: ActivityStatus
    message: str = Field(min_length=1)
    details: tuple[str, ...] = ()


class Message(BaseModel):
    """One API-compatible message, including optional tool content."""

    model_config = ConfigDict(frozen=True)

    role: MessageRole
    content: str = ""
    thinking: tuple[ThinkingContent, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()

    @model_validator(mode="after")
    def validate_content_for_role(self) -> Message:
        """Keep user, assistant, and tool content in valid combinations."""

        if not self.content.strip() and not self.tool_calls and not self.tool_results:
            raise ValueError("message must contain text or tool content")
        if self.role == "user" and (self.thinking or self.tool_calls):
            raise ValueError("user messages cannot contain thinking or tool calls")
        if self.role == "assistant" and self.tool_results:
            raise ValueError("assistant messages cannot contain tool results")
        return self

    def to_api_dict(self) -> dict[str, Any]:
        """Convert the message to the shape expected by the model API."""

        if not self.thinking and not self.tool_calls and not self.tool_results:
            return {"role": self.role, "content": self.content}

        blocks: list[dict[str, Any]] = []
        blocks.extend(item.to_api_dict() for item in self.thinking)
        if self.content:
            blocks.append({"type": "text", "text": self.content})
        blocks.extend(item.to_api_dict() for item in self.tool_calls)
        blocks.extend(item.to_api_dict() for item in self.tool_results)
        return {"role": self.role, "content": blocks}


class ThinkingContent(BaseModel):
    """Thinking content that must be replayed during a tool-use turn."""

    model_config = ConfigDict(frozen=True)

    thinking: str
    signature: str

    def to_api_dict(self) -> dict[str, str]:
        return {
            "type": "thinking",
            "thinking": self.thinking,
            "signature": self.signature,
        }


class ToolCall(BaseModel):
    """A tool invocation requested by the model."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "type": "tool_use",
            "id": self.id,
            "name": self.name,
            "input": self.arguments,
        }


class ToolResult(BaseModel):
    """The result returned after executing a tool call."""

    model_config = ConfigDict(frozen=True)

    tool_call_id: str = Field(min_length=1)
    content: str
    is_error: bool = False

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": self.tool_call_id,
            "content": self.content,
            "is_error": self.is_error,
        }


class ToolDefinition(BaseModel):
    """A tool description and JSON input schema exposed to the model."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: dict[str, Any]

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ModelResponse(BaseModel):
    """The accumulated response produced by one model request."""

    model_config = ConfigDict(frozen=True)

    content: str = ""
    thinking: tuple[ThinkingContent, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
