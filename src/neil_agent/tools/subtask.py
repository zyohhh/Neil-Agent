"""Read-only subtask tool registered on standard CLI and Web runtimes."""

from __future__ import annotations

from ..errors import ToolError
from ..schemas import ToolDefinition
from ..subtask import execute_readonly_subtask
from .registry import RuntimeDisposer, ToolRegistry


def run_readonly_subtask_definition(max_prompt_chars: int) -> ToolDefinition:
    """Build the model-facing schema bound to the configured prompt limit."""

    return ToolDefinition(
        name="run_readonly_subtask",
        description=(
            "Spawn a one-shot read-only sub-agent to explore the repository in parallel. "
            "The sub-agent may only use read-only filesystem tools and returns a bounded "
            "summary without merging its full conversation into the main history."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": max_prompt_chars,
                    "description": (
                        "Focused read-only exploration instructions for the sub-agent."
                    ),
                }
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    )


class ReadonlySubtaskTools:
    """Register the synchronous read-only subtask tool on one host runtime."""

    def run_readonly_subtask(self, prompt: str) -> str:
        try:
            return execute_readonly_subtask(prompt)
        except ToolError:
            raise
        except Exception as error:  # noqa: BLE001 - tool boundary
            raise ToolError(f"只读子任务失败：{type(error).__name__}") from error

    def register(
        self,
        registry: ToolRegistry,
        *,
        max_prompt_chars: int,
    ) -> RuntimeDisposer:
        return registry.register(
            run_readonly_subtask_definition(max_prompt_chars),
            self.run_readonly_subtask,
        )
