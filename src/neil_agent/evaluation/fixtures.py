"""Deterministic models for offline harness verification."""

from __future__ import annotations

from collections.abc import Iterator, Sequence


from ..errors import AgentError
from ..schemas import (
    Message,
    ModelResponse,
    ToolCall,
    ToolDefinition,
)


class OfflineModel:
    """Deterministic in-process model used by offline evaluations."""

    def __init__(self, response: str = "offline response") -> None:
        self.response = response
        self.system_prompts: list[str] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        self.system_prompts.append(system_prompt)
        return self.response

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self.system_prompts.append(system_prompt)
        yield self.response
        yield ModelResponse(content=self.response)


class FailingCompactionModel(OfflineModel):
    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        raise AgentError("simulated compaction failure")


class ApprovalWorkflowModel(OfflineModel):
    """Request a guarded write and then finish after its denied result."""

    def __init__(self) -> None:
        super().__init__("workflow complete")
        self.calls = 0

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self.calls += 1
        if self.calls == 1:
            yield ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="offline-write",
                        name="write_file",
                        arguments={"path": "result.txt", "content": "blocked"},
                    ),
                )
            )
            return
        yield self.response
        yield ModelResponse(content=self.response)
