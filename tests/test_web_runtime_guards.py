"""The real Web worker binds cancellation before Agent tools can execute."""

from collections.abc import Iterator, Sequence
from pathlib import Path
from threading import Event

import pytest

from neil_agent.config import Settings
from neil_agent.errors import ToolError
from neil_agent.schemas import Message, ModelResponse, ToolCall, ToolDefinition
from neil_agent.tools.filesystem import FileSystemTools
from neil_agent.web import controller as controller_module
from neil_agent.web.controller import AgentTurnWorker


def test_web_cancel_without_visible_text_stops_tools_and_followup_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = Event()
    executions: list[str] = []

    class CancelModel:
        requests = 0

        def complete(self, messages: Sequence[Message], *, system_prompt: str) -> str:
            raise AssertionError("No complete request expected")

        def stream(
            self,
            messages: Sequence[Message],
            *,
            system_prompt: str,
            tools: Sequence[ToolDefinition] = (),
        ) -> Iterator[str | ModelResponse]:
            self.requests += 1
            if self.requests == 1:
                cancel.set()
                yield ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="read-after-cancel",
                            name="read_file",
                            arguments={"path": "note.txt"},
                        ),
                    )
                )
            else:
                yield "done"
                yield ModelResponse(content="done")

    model = CancelModel()
    monkeypatch.setattr(
        controller_module, "create_provider", lambda *_args, **_kwargs: model
    )
    monkeypatch.setattr(
        FileSystemTools,
        "read_file",
        lambda self, path: executions.append(path) or "contents",
    )
    settings = Settings(
        _env_file=None, workspace_root=tmp_path, deepseek_api_key="offline-test"
    )
    with pytest.raises(ToolError, match="当前任务已取消"):
        AgentTurnWorker(settings).run(
            "read note.txt",
            None,
            cancel,
            lambda _: None,
            lambda _: None,
            lambda _: None,
            lambda *_: False,
            parent_run_id="run-cancel-test",
        )
    assert executions == []
    assert model.requests == 1
