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
    SUPPORTED_ERROR_CODES_V2,
    run_noninteractive,
)
from neil_agent.schemas import (
    Message,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)


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


class WriteFileModel(OneShotFakeModel):
    def __init__(self, *, content: str = "created by approval") -> None:
        super().__init__()
        self.content = content
        self.model_round = 0

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self.tools = [definition.name for definition in tools]
        self.model_round += 1
        if self.model_round == 1:
            yield ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="write-call",
                        name="write_file",
                        arguments={
                            "path": "approved.txt",
                            "content": self.content,
                        },
                    ),
                ),
                usage=TokenUsage(input_tokens=10, output_tokens=2),
            )
            return
        yield "write handled"
        yield ModelResponse(
            content="write handled",
            usage=TokenUsage(input_tokens=5, output_tokens=2),
        )


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


def _protocol_v2_contract() -> dict[str, object]:
    fixture_path = (
        Path(__file__).parent / "fixtures" / "noninteractive_protocol_v2.json"
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


def test_protocol_v2_requests_then_consumes_one_exact_write_approval(
    tmp_path: Path,
) -> None:
    prompt = "create the approved file"
    request_stdout = StringIO()

    request_exit = run_noninteractive(
        _settings(tmp_path),
        prompt,
        output_format="json",
        stdout=request_stdout,
        stderr=StringIO(),
        save_session=True,
        protocol_version=2,
        permission_mode="request",
        llm=WriteFileModel(),
    )

    request_payload = json.loads(request_stdout.getvalue())
    contract = _protocol_v2_contract()
    approval = request_payload["approval_requests"][0]
    assert request_exit == 3
    assert request_payload["type"] == "approval_required"
    assert request_payload["protocol_version"] == 2
    assert request_payload["permission_mode"] == "request"
    assert approval["tool_name"] == "write_file"
    assert "approved.txt" in approval["preview"]
    assert set(request_payload) == set(contract["json_approval_required"])
    assert set(approval) == set(contract["approval_request"])
    assert not (tmp_path / "approved.txt").exists()
    assert not (tmp_path / ".neil-agent" / "sessions").exists()

    approval_stdout = StringIO()
    approval_exit = run_noninteractive(
        _settings(tmp_path),
        prompt,
        output_format="json",
        stdout=approval_stdout,
        stderr=StringIO(),
        protocol_version=2,
        permission_mode="approve",
        approval_id=approval["approval_id"],
        llm=WriteFileModel(),
    )

    approval_payload = json.loads(approval_stdout.getvalue())
    assert approval_exit == 0
    assert approval_payload["type"] == "result"
    assert approval_payload["approved_request_id"] == approval["approval_id"]
    assert set(approval_payload) == set(contract["json_result"])
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == (
        "created by approval"
    )

    replay_stdout = StringIO()
    replay_exit = run_noninteractive(
        _settings(tmp_path),
        prompt,
        output_format="json",
        stdout=replay_stdout,
        stderr=StringIO(),
        protocol_version=2,
        permission_mode="approve",
        approval_id=approval["approval_id"],
        llm=WriteFileModel(),
    )
    replay_payload = json.loads(replay_stdout.getvalue())
    assert replay_exit == 1
    assert replay_payload["error_code"] == "approval_error"
    assert "重放" in replay_payload["error"]


def test_protocol_v2_rejects_stale_preview_and_issues_a_new_request(
    tmp_path: Path,
) -> None:
    prompt = "create the approved file"
    request_stdout = StringIO()
    run_noninteractive(
        _settings(tmp_path),
        prompt,
        output_format="json",
        stdout=request_stdout,
        stderr=StringIO(),
        protocol_version=2,
        permission_mode="request",
        llm=WriteFileModel(),
    )
    request_payload = json.loads(request_stdout.getvalue())
    approval_id = request_payload["approval_requests"][0]["approval_id"]
    target = tmp_path / "approved.txt"
    target.write_text("external change", encoding="utf-8")
    approval_stdout = StringIO()

    exit_code = run_noninteractive(
        _settings(tmp_path),
        prompt,
        output_format="json",
        stdout=approval_stdout,
        stderr=StringIO(),
        protocol_version=2,
        permission_mode="approve",
        approval_id=approval_id,
        llm=WriteFileModel(),
    )

    payload = json.loads(approval_stdout.getvalue())
    assert exit_code == 3
    assert payload["type"] == "approval_required"
    assert payload["approved_request_id"] is None
    assert payload["approval_requests"][0]["approval_id"] != approval_id
    assert target.read_text(encoding="utf-8") == "external change"


def test_protocol_v2_write_modes_require_structured_output_and_explicit_id(
    tmp_path: Path,
) -> None:
    cases = (
        ("text", 2, "request", None),
        ("json", 1, "request", None),
        ("json", 2, "approve", None),
    )
    for output_format, version, permission_mode, approval_id in cases:
        stdout = StringIO()
        exit_code = run_noninteractive(
            _settings(tmp_path),
            "update",
            output_format=output_format,
            stdout=stdout,
            stderr=StringIO(),
            protocol_version=version,
            permission_mode=permission_mode,
            approval_id=approval_id,
            llm=WriteFileModel(),
        )
        assert exit_code == 2
        assert not (tmp_path / "approved.txt").exists()


def test_protocol_v2_stream_emits_preview_before_terminal_event(
    tmp_path: Path,
) -> None:
    stdout = StringIO()

    exit_code = run_noninteractive(
        _settings(tmp_path),
        "create the approved file",
        output_format="stream-json",
        stdout=stdout,
        stderr=StringIO(),
        protocol_version=2,
        permission_mode="request",
        llm=WriteFileModel(),
    )

    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    contract = _protocol_v2_contract()
    event_types = [event["type"] for event in events]
    assert exit_code == 3
    assert events[0]["permission_mode"] == "request"
    assert events[0]["read_only"] is False
    assert "approval_request" in event_types
    assert event_types[-1] == "approval_required"
    assert event_types.index("approval_request") < len(event_types) - 1
    assert contract["error_codes"] == list(SUPPORTED_ERROR_CODES_V2)
    for event in events:
        assert set(event) == set(contract["events"][event["type"]])
