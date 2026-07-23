"""Tests for the one-shot read-only output protocols."""

from collections.abc import Iterator, Sequence
from io import StringIO
import json
from pathlib import Path

from neil_agent.config import Settings
from neil_agent.errors import LLMError
from neil_agent.noninteractive import (
    PROTOCOL_VERSION,
    SUPPORTED_ERROR_CODES,
    run_noninteractive,
)
from neil_agent.schemas import Message, ModelResponse, TokenUsage, ToolDefinition


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
        yield ModelResponse(
            content="hello world",
            usage=TokenUsage(input_tokens=8, output_tokens=2),
        )


class ExpectedFailureModel(OneShotFakeModel):
    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        raise LLMError("model unavailable")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=tmp_path,
    )


def _protocol_contract() -> dict[str, object]:
    fixture_path = (
        Path(__file__).parent / "fixtures" / "noninteractive_protocol_v1.json"
    )
    return json.loads(fixture_path.read_text(encoding="utf-8"))


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
    assert payload["usage"]["input_tokens"] == 8
    assert payload["usage"]["total_tokens"] == 10
    assert isinstance(payload["activities"], list)
    contract = _protocol_contract()
    assert set(payload) == set(contract["json_result"])
    assert set(payload["usage"]) == set(contract["usage"])
    assert all(
        set(activity) == set(contract["json_activity"])
        for activity in payload["activities"]
    )
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
    assert events[-1]["usage"]["total_tokens"] == 10


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
    assert payload["error_code"] == "internal_error"
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
    assert payload["error_code"] == "configuration_error"


def test_expected_model_error_has_stable_error_code(tmp_path: Path) -> None:
    stdout = StringIO()

    exit_code = run_noninteractive(
        _settings(tmp_path),
        "fail",
        output_format="json",
        stdout=stdout,
        stderr=StringIO(),
        llm=ExpectedFailureModel(),
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == 1
    assert payload["error_code"] == "model_error"


def test_stream_protocol_matches_versioned_contract_fixture(tmp_path: Path) -> None:
    contract = _protocol_contract()
    stdout = StringIO()

    run_noninteractive(
        _settings(tmp_path),
        "say hello",
        output_format="stream-json",
        stdout=stdout,
        stderr=StringIO(),
        llm=OneShotFakeModel(),
    )

    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert contract["protocol_version"] == PROTOCOL_VERSION
    assert contract["error_codes"] == list(SUPPORTED_ERROR_CODES)
    for event in events:
        assert set(event) == set(contract["events"][event["type"]])
        if event["type"] == "result":
            assert set(event["usage"]) == set(contract["usage"])


def test_error_event_matches_contract_fixture(tmp_path: Path) -> None:
    stdout = StringIO()

    run_noninteractive(
        _settings(tmp_path),
        "fail",
        output_format="stream-json",
        stdout=stdout,
        stderr=StringIO(),
        llm=ExpectedFailureModel(),
    )

    contract = _protocol_contract()
    error = json.loads(stdout.getvalue().splitlines()[-1])
    assert error["type"] == "error"
    assert set(error) == set(contract["events"]["error"])
    assert error["error_code"] in contract["error_codes"]


def test_enabled_audit_log_records_metadata_without_prompt(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(update={"audit_log_enabled": True})
    stdout = StringIO()
    secret_prompt = "PRIVATE-PROMPT-CONTENT"

    exit_code = run_noninteractive(
        settings,
        secret_prompt,
        output_format="json",
        stdout=stdout,
        stderr=StringIO(),
        llm=OneShotFakeModel(),
    )

    audit_path = tmp_path / ".neil-agent" / "audit" / "events.jsonl"
    audit_text = audit_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in audit_text.splitlines()]
    assert exit_code == 0
    assert secret_prompt not in audit_text
    assert [record["stage"] for record in records] == [
        "before_model",
        "after_model",
    ]


def test_unsafe_audit_path_has_stable_audit_error_code(tmp_path: Path) -> None:
    audit_path = tmp_path / ".neil-agent" / "audit"
    audit_path.parent.mkdir()
    audit_path.write_text("not a directory", encoding="utf-8")
    settings = _settings(tmp_path).model_copy(update={"audit_log_enabled": True})
    stdout = StringIO()

    exit_code = run_noninteractive(
        settings,
        "inspect",
        output_format="json",
        stdout=stdout,
        stderr=StringIO(),
        llm=OneShotFakeModel(),
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == 1
    assert payload["error_code"] == "audit_error"
