"""Tests for the one-shot read-only output protocols."""

from collections.abc import Iterator, Sequence
from io import StringIO
import json
from pathlib import Path

from neil_agent.config import Settings
from neil_agent.noninteractive import run_noninteractive
from neil_agent.schemas import Message, ModelResponse, ToolDefinition


class OneShotFakeModel:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.tools: list[str] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        raise NotImplementedError

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self.tools = [definition.name for definition in tools]
        if self.fail:
            raise RuntimeError("SECRET INTERNAL DETAIL")
        yield "hello "
        yield "world"
        yield ModelResponse(content="hello world")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=tmp_path,
    )


def test_text_output_is_plain_and_one_shot_tools_are_read_only(tmp_path: Path) -> None:
    stdout = StringIO()
    stderr = StringIO()
    model = OneShotFakeModel()

    exit_code = run_noninteractive(
        _settings(tmp_path),
        "say hello",
        output_format="text",
        stdout=stdout,
        stderr=stderr,
        llm=model,
    )

    assert exit_code == 0
    assert stdout.getvalue() == "hello world\n"
    assert stderr.getvalue() == ""
    assert model.tools == [
        "list_directory",
        "read_file",
        "search_text",
        "git_status",
        "git_diff",
    ]


def test_json_output_is_one_document_and_does_not_save_by_default(
    tmp_path: Path,
) -> None:
    stdout = StringIO()
    stderr = StringIO()

    exit_code = run_noninteractive(
        _settings(tmp_path),
        "say hello",
        output_format="json",
        stdout=stdout,
        stderr=stderr,
        llm=OneShotFakeModel(),
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert payload["type"] == "result"
    assert payload["protocol_version"] == 1
    assert payload["result"] == "hello world"
    assert payload["saved"] is False
    assert isinstance(payload["activities"], list)
    assert stderr.getvalue() == ""
    assert not (tmp_path / ".neil-agent" / "sessions").exists()


def test_stream_json_emits_json_lines_in_protocol_order(tmp_path: Path) -> None:
    stdout = StringIO()

    exit_code = run_noninteractive(
        _settings(tmp_path),
        "say hello",
        output_format="stream-json",
        stdout=stdout,
        stderr=StringIO(),
        llm=OneShotFakeModel(),
    )

    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    event_types = [event["type"] for event in events]
    assert exit_code == 0
    assert event_types[0] == "session_start"
    assert event_types[-1] == "result"
    assert event_types.count("text_delta") == 2
    assert events[0]["read_only"] is True
    assert events[-1]["success"] is True


def test_structured_runtime_error_is_sanitized_and_has_explicit_exit(
    tmp_path: Path,
) -> None:
    stdout = StringIO()

    exit_code = run_noninteractive(
        _settings(tmp_path),
        "fail",
        output_format="json",
        stdout=stdout,
        stderr=StringIO(),
        llm=OneShotFakeModel(fail=True),
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == 1
    assert payload["type"] == "error"
    assert payload["exit_code"] == 1
    assert "SECRET INTERNAL DETAIL" not in payload["error"]


def test_save_session_is_explicit_opt_in(tmp_path: Path) -> None:
    stdout = StringIO()

    exit_code = run_noninteractive(
        _settings(tmp_path),
        "save this",
        output_format="json",
        stdout=stdout,
        stderr=StringIO(),
        save_session=True,
        llm=OneShotFakeModel(),
    )

    payload = json.loads(stdout.getvalue())
    session_file = (
        tmp_path / ".neil-agent" / "sessions" / f"{payload['session_id']}.json"
    )
    assert exit_code == 0
    assert payload["saved"] is True
    assert session_file.is_file()


def test_invalid_workspace_is_a_configuration_exit(tmp_path: Path) -> None:
    stdout = StringIO()
    settings = _settings(tmp_path)
    settings.workspace_root = tmp_path / "missing"

    exit_code = run_noninteractive(
        settings,
        "inspect",
        output_format="json",
        stdout=stdout,
        stderr=StringIO(),
        llm=OneShotFakeModel(),
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == 2
    assert payload["exit_code"] == 2
