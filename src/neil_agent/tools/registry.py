"""Tool registration, lookup, and safe dispatch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from inspect import signature
from typing import Literal

from ..approval import ApprovalBinding, ApprovalBindingResolver
from ..errors import ToolError
from ..schemas import ToolCall, ToolDefinition, ToolResult

ToolHandler = Callable[..., str]
ToolPreviewHandler = Callable[..., str]
ApprovedPreviewBinding = Literal["valid", "changed", "unavailable"]
RuntimeDisposer = Callable[[], None]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    definition: ToolDefinition
    handler: ToolHandler
    requires_approval: bool = False
    preview_handler: ToolPreviewHandler | None = None
    binding_resolver: ApprovalBindingResolver | None = None


@dataclass(frozen=True, slots=True)
class ApprovedToolExecution:
    """One approved execution plus its fail-closed preview-binding result."""

    result: ToolResult
    preview_binding: ApprovedPreviewBinding


class ToolRegistry:
    """Store tool definitions and dispatch validated calls by name."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        definition: ToolDefinition,
        handler: ToolHandler,
        *,
        requires_approval: bool = False,
        preview_handler: ToolPreviewHandler | None = None,
        binding_resolver: ApprovalBindingResolver | None = None,
    ) -> RuntimeDisposer:
        """Register one tool, rejecting ambiguous duplicate names."""

        if definition.name in self._tools:
            raise ValueError(f"tool already registered: {definition.name}")
        if requires_approval and preview_handler is None:
            raise ValueError("approval-required tools must provide a preview handler")
        self._tools[definition.name] = RegisteredTool(
            definition=definition,
            handler=handler,
            requires_approval=requires_approval,
            preview_handler=preview_handler,
            binding_resolver=binding_resolver,
        )
        name = definition.name

        def dispose() -> None:
            self.unregister(name)

        return dispose

    def unregister(self, tool_name: str) -> None:
        """Remove one tool registration when its host runtime is torn down."""

        self._tools.pop(tool_name, None)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return model-facing definitions in registration order."""

        return tuple(tool.definition for tool in self._tools.values())

    def requires_approval(self, tool_name: str) -> bool:
        """Return whether a registered tool requires explicit user approval."""

        registered = self._tools.get(tool_name)
        return registered is not None and registered.requires_approval

    def resolve_approval_binding(
        self,
        call: ToolCall,
        preview: str,
    ) -> ApprovalBinding | None:
        """Return a tool-specific approval binding when one is registered."""

        registered = self._tools.get(call.name)
        if registered is None or registered.binding_resolver is None:
            return None
        return registered.binding_resolver(call, preview)

    def preview(self, call: ToolCall) -> ToolResult:
        """Build the user-facing preview for an approval-required tool."""

        registered = self._tools.get(call.name)
        if registered is None:
            return self._error(call, f"未知工具：{call.name}")
        if registered.preview_handler is None:
            return self._error(call, f"工具不支持预览：{call.name}")
        return self._invoke(call, registered.preview_handler)

    def execute(
        self,
        call: ToolCall,
        *,
        approved: bool = False,
        approved_preview: str | None = None,
    ) -> ToolResult:
        """Execute one tool call and always return a structured result."""

        registered = self._tools.get(call.name)
        if registered is None:
            return self._error(call, f"未知工具：{call.name}")
        if registered.requires_approval:
            if not approved:
                return self._error(call, f"工具需要用户确认后才能执行：{call.name}")
            if approved_preview is None:
                return self._error(call, f"工具缺少已确认的操作预览：{call.name}")
            return self.execute_approved(
                call,
                approved_preview=approved_preview,
            ).result

        return self._invoke(call, registered.handler)

    def execute_approved(
        self,
        call: ToolCall,
        *,
        approved_preview: str,
    ) -> ApprovedToolExecution:
        """Revalidate one approved preview and report binding without exposing it."""

        registered = self._tools.get(call.name)
        if registered is None:
            return ApprovedToolExecution(
                self._error(call, f"未知工具：{call.name}"),
                "unavailable",
            )
        if not registered.requires_approval or registered.preview_handler is None:
            return ApprovedToolExecution(
                self._error(call, f"工具不支持批准后执行：{call.name}"),
                "unavailable",
            )
        current_preview = self._invoke(call, registered.preview_handler)
        if current_preview.is_error:
            return ApprovedToolExecution(current_preview, "unavailable")
        if current_preview.content != approved_preview:
            return ApprovedToolExecution(
                self._error(call, "操作预览在确认后发生变化，请重新预览并确认。"),
                "changed",
            )
        return ApprovedToolExecution(self._invoke(call, registered.handler), "valid")

    def _invoke(self, call: ToolCall, handler: ToolHandler) -> ToolResult:
        """Validate arguments, invoke a handler, and normalize its result."""

        try:
            signature(handler).bind(**call.arguments)
        except TypeError as error:
            return self._error(call, f"工具参数错误：{error}")

        try:
            content = handler(**call.arguments)
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
