"""Tool registration, lookup, and safe dispatch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from inspect import signature

from ..errors import ToolError
from ..schemas import ToolCall, ToolDefinition, ToolResult

ToolHandler = Callable[..., str]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    definition: ToolDefinition
    handler: ToolHandler


class ToolRegistry:
    """Store tool definitions and dispatch validated calls by name."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, definition: ToolDefinition, handler: ToolHandler) -> None:
        """Register one tool, rejecting ambiguous duplicate names."""

        if definition.name in self._tools:
            raise ValueError(f"tool already registered: {definition.name}")
        self._tools[definition.name] = RegisteredTool(definition, handler)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return model-facing definitions in registration order."""

        return tuple(tool.definition for tool in self._tools.values())

    def execute(self, call: ToolCall) -> ToolResult:
        """Execute one tool call and always return a structured result."""

        registered = self._tools.get(call.name)
        if registered is None:
            return self._error(call, f"未知工具：{call.name}")

        try:
            signature(registered.handler).bind(**call.arguments)
        except TypeError as error:
            return self._error(call, f"工具参数错误：{error}")

        try:
            content = registered.handler(**call.arguments)
        except ToolError as error:
            return self._error(call, str(error))
        except OSError as error:
            return self._error(call, f"文件操作失败：{error}")
        except Exception:
            return self._error(call, "工具执行失败，请检查参数或实现。")

        return ToolResult(tool_call_id=call.id, content=content)

    @staticmethod
    def _error(call: ToolCall, message: str) -> ToolResult:
        return ToolResult(
            tool_call_id=call.id,
            content=message,
            is_error=True,
        )
