"""Write-ahead facts survive failed turns without replaying tool effects."""

from collections.abc import Iterator, Sequence
from io import StringIO
from pathlib import Path
from threading import Event

import pytest
from rich.console import Console

from neil_agent.agent import Agent
from neil_agent import cli, execution_journal as journal_module
from neil_agent.config import Settings
from neil_agent.errors import AgentError, ExecutionJournalError, LLMError, ToolError
from neil_agent.execution_budget import execution_budget_scope
from neil_agent.execution_journal import ExecutionJournal
from neil_agent.hooks import LifecycleHooks
from neil_agent.host_runtime import HostMode, RuntimeProfile, build_host_runtime
from neil_agent.schemas import Message, ModelResponse, ToolCall, ToolDefinition
from neil_agent.session import SessionStore
from neil_agent.task import TaskTracker
from neil_agent.tools.registry import ToolRegistry
from neil_agent.noninteractive import run_noninteractive


class ScriptModel:
    def __init__(self, *rounds: list[str | ModelResponse] | Exception) -> None:
        self.rounds = list(rounds)
        self.prompts: list[str] = []

    def complete(self, messages: Sequence[Message], *, system_prompt: str) -> str:
        self.prompts.append(system_prompt)
        return "done"

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        self.prompts.append(system_prompt)
        events = self.rounds.pop(0)
        if isinstance(events, Exception):
            raise events
        yield from events


def _write_round() -> list[ModelResponse]:
    return [
        ModelResponse(
            tool_calls=(
                ToolCall(
                    id="private-call-id",
                    name="write_file",
                    arguments={"path": "note.txt", "content": "PRIVATE-FILE-BODY"},
                ),
            )
        )
    ]


def _registry(root: Path, journal: ExecutionJournal) -> ToolRegistry:
    registry = ToolRegistry()

    def write_file(path: str, content: str) -> str:
        # This assertion is inside the mutation handler, not an after-call probe.
        operation = journal.records()[0].operations[-1]
        assert (
            operation.started and operation.approved and operation.status == "started"
        )
        (root / path).write_text(content, encoding="utf-8")
        return "PRIVATE-TOOL-OUTPUT"

    registry.register(
        ToolDefinition(
            name="write_file", description="Write a test file", input_schema={}
        ),
        write_file,
        requires_approval=True,
        preview_handler=lambda **_: "PRIVATE-PREVIEW",
    )
    return registry


def test_failed_turn_preserves_actual_effect_and_restart_context(
    tmp_path: Path,
) -> None:
    journal = ExecutionJournal(tmp_path)
    model = ScriptModel(_write_round(), LLMError("PRIVATE-PROVIDER-ERROR"))
    agent = Agent(
        model,
        registry=_registry(tmp_path, journal),
        execution_journal=journal,
        approval_handler=lambda *_: True,
    )
    with pytest.raises(LLMError):
        list(agent.stream_chat("PRIVATE-PROMPT"))
    assert agent.messages == ()
    assert (tmp_path / "note.txt").read_text() == "PRIVATE-FILE-BODY"
    record = journal.records()[0]
    assert record.status == "failed" and record.needs_reconciliation
    assert record.operations[0].status == "succeeded"
    payload = (journal.root / f"{record.execution_id}.json").read_text()
    for secret in ("PRIVATE", "private-call-id"):
        assert secret not in payload
    assert record.operations[0].target == "note.txt"

    restarted_model = ScriptModel(["done", ModelResponse(content="done")])
    restarted = Agent(restarted_model, execution_journal=ExecutionJournal(tmp_path))
    assert "".join(restarted.stream_chat("continue")) == "done"
    assert record.execution_id in restarted_model.prompts[0]
    assert "never automatically replay" in restarted_model.prompts[0]
    assert (tmp_path / "note.txt").read_text() == "PRIVATE-FILE-BODY"


def test_journal_write_failure_prevents_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = ExecutionJournal(tmp_path)
    original = journal.operation

    def fail_started(execution_id, index, status):
        if status == "started":
            raise ExecutionJournalError("disk unavailable")
        return original(execution_id, index, status)

    monkeypatch.setattr(journal, "operation", fail_started)
    agent = Agent(
        ScriptModel(_write_round()),
        registry=_registry(tmp_path, journal),
        execution_journal=journal,
        approval_handler=lambda *_: True,
    )
    with pytest.raises(ExecutionJournalError, match="disk unavailable"):
        list(agent.stream_chat("write"))
    assert not (tmp_path / "note.txt").exists()
    assert journal.records()[0].operations[0].status == "approved"
    assert not journal.records()[0].needs_reconciliation


def test_crash_after_started_is_uncertain_until_explicit_acknowledgement(
    tmp_path: Path,
) -> None:
    journal = ExecutionJournal(tmp_path)
    execution_id = journal.begin()
    index = journal.request(
        execution_id, "replace_text", requires_approval=True, target="note.txt"
    )
    journal.operation(execution_id, index, "approved")
    journal.operation(execution_id, index, "started")
    reopened = ExecutionJournal(tmp_path)
    assert execution_id in reopened.recovery_context()
    reopened.finish(execution_id, "acknowledged")
    assert reopened.recovery_context() == ""
    # A live process progressing after an acknowledgement makes it pending again.
    journal.operation(execution_id, index, "succeeded")
    assert reopened.load(execution_id).needs_reconciliation


def test_success_settles_only_after_session_save(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.new_session()
    journal = ExecutionJournal(tmp_path)
    agent = Agent(
        ScriptModel(_write_round(), ["done", ModelResponse(content="done")]),
        registry=_registry(tmp_path, journal),
        execution_journal=journal,
        approval_handler=lambda *_: True,
    )
    agent.bind_session(session.session_id)
    list(agent.stream_chat("write"))
    execution_id = agent.last_execution_id
    assert execution_id is not None
    assert journal.load(execution_id).status == "completed"
    saved_handle = cli._save_current_session(
        Console(file=None, quiet=True), store, session, agent, TaskTracker()
    )
    assert saved_handle.base_digest is not None
    assert journal.load(execution_id).status == "committed"
    assert journal.recovery_context() == ""


def test_session_conflict_leaves_execution_pending(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    saved = store.save(
        store.new_session(),
        (Message(role="user", content="old"), Message(role="assistant", content="old")),
        (),
        None,
    )
    handle = store.handle_for(saved)
    journal = ExecutionJournal(tmp_path)
    agent = Agent(
        ScriptModel(_write_round(), ["done", ModelResponse(content="done")]),
        registry=_registry(tmp_path, journal),
        execution_journal=journal,
        approval_handler=lambda *_: True,
    )
    agent.bind_session(handle.session_id)
    list(agent.stream_chat("write"))
    latest = store.rename(handle.session_id, "other process")
    assert (
        cli._save_current_session(
            Console(quiet=True), store, handle, agent, TaskTracker()
        )
        == handle
    )
    assert store.load(handle.session_id) == latest
    assert journal.records()[0].status == "save_failed"
    assert journal.records()[0].needs_reconciliation


def test_cancellation_is_logged_without_committing_history(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path)
    cancel = Event()
    agent = Agent(
        ScriptModel(_write_round()),
        registry=_registry(tmp_path, journal),
        execution_journal=journal,
        approval_handler=lambda *_: cancel.set() or True,
    )
    with execution_budget_scope(cancel=cancel, cancelled_message="cancelled"):
        with pytest.raises(ToolError, match="cancelled"):
            list(agent.stream_chat("write"))
    assert journal.records()[0].status == "cancelled"
    assert not (tmp_path / "note.txt").exists()
    assert agent.messages == ()


def test_post_hook_failure_does_not_erase_successful_tool_fact(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path)
    hooks = LifecycleHooks()

    def fail(_):
        raise OSError("audit failed")

    hooks.register("after_tool", fail)
    agent = Agent(
        ScriptModel(_write_round(), LLMError("failed")),
        hooks=hooks,
        registry=_registry(tmp_path, journal),
        execution_journal=journal,
        approval_handler=lambda *_: True,
    )
    with pytest.raises(LLMError):
        list(agent.stream_chat("write"))
    assert journal.records()[0].operations[0].status == "succeeded"
    assert journal.records()[0].needs_reconciliation


def test_rejected_approval_records_no_effect(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path)
    agent = Agent(
        ScriptModel(_write_round(), ["declined", ModelResponse(content="declined")]),
        registry=_registry(tmp_path, journal),
        execution_journal=journal,
        approval_handler=lambda *_: False,
    )
    list(agent.stream_chat("write"))
    operation = journal.records()[0].operations[0]
    assert (
        operation.status == "skipped"
        and not operation.started
        and not operation.approved
    )
    assert journal.recovery_context() == ""


def test_bounded_retention_keeps_unreconciled_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(journal_module, "MAX_EXECUTION_RECORDS", 2)
    journal = ExecutionJournal(tmp_path)
    pending = journal.begin()
    index = journal.request(pending, "write_file", requires_approval=True)
    journal.operation(pending, index, "started")
    journal.finish(pending, "failed")
    safe = journal.begin()
    journal.finish(safe, "committed")
    newest = journal.begin()
    assert {record.execution_id for record in journal.records()} == {pending, newest}
    with pytest.raises(ExecutionJournalError, match="已满"):
        journal.begin()
    assert journal.load(pending).needs_reconciliation


@pytest.mark.parametrize(
    "target", ["../outside.txt", ".env", ".git/config", "bad\nname", "C:\\outside.txt"]
)
def test_secret_and_outside_paths_are_not_persisted(
    tmp_path: Path, target: str
) -> None:
    journal = ExecutionJournal(tmp_path)
    execution_id = journal.begin()
    journal.request(execution_id, "write_file", requires_approval=True, target=target)
    assert journal.load(execution_id).operations[0].target is None


def test_corrupt_journal_fails_closed_before_new_turn(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path)
    execution_id = journal.begin()
    (journal.root / f"{execution_id}.json").write_text("invalid", encoding="utf-8")
    agent = Agent(ScriptModel(), execution_journal=journal)
    with pytest.raises(ExecutionJournalError, match="读取失败"):
        agent.chat("hello")


def test_closed_store_allows_inflight_settlement_but_rejects_new_turns(
    tmp_path: Path,
) -> None:
    journal = ExecutionJournal(tmp_path)
    execution_id = journal.begin()
    journal.close()
    journal.finish(execution_id, "cancelled")
    assert journal.load(execution_id).status == "cancelled"
    with pytest.raises(ExecutionJournalError, match="已关闭"):
        journal.begin()


@pytest.mark.parametrize(
    "mode,profile,enabled",
    [
        (HostMode.CLI, RuntimeProfile.STANDARD, True),
        (HostMode.WEB, RuntimeProfile.WEB_SAFE, True),
        (HostMode.NONINTERACTIVE_WRITE, RuntimeProfile.BENCHMARK_MINIMAL, True),
        (HostMode.NONINTERACTIVE_READONLY, RuntimeProfile.STANDARD, False),
        (HostMode.CLI, RuntimeProfile.READONLY_SUBTASK, False),
    ],
)
def test_journal_assembly_is_lazy_and_respects_readonly_profiles(
    tmp_path: Path, mode, profile, enabled
) -> None:
    runtime = build_host_runtime(
        Settings(_env_file=None, workspace_root=tmp_path, deepseek_api_key="test"),
        mode=mode,
        profile=profile,
    )
    try:
        assert (runtime.execution_journal is not None) is enabled
        assert not (tmp_path / ".neil-agent").exists()
    finally:
        runtime.close()


def test_same_agent_rejects_overlapping_turns(tmp_path: Path) -> None:
    agent = Agent(
        ScriptModel(["partial", ModelResponse(content="done")]),
        execution_journal=ExecutionJournal(tmp_path),
    )
    first = agent.stream_chat("one")
    assert next(first) == "partial"
    with pytest.raises(AgentError, match="正在执行"):
        agent.chat("two")
    first.close()


def test_atomic_journal_replace_failure_stops_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    journal = ExecutionJournal(tmp_path)
    original_replace = journal_module.os.replace

    def fail_started(source, target):
        record = json.loads(Path(source).read_text(encoding="utf-8"))
        if any(operation["status"] == "started" for operation in record["operations"]):
            raise OSError("disk failure")
        return original_replace(source, target)

    monkeypatch.setattr(journal_module.os, "replace", fail_started)
    agent = Agent(
        ScriptModel(_write_round()),
        registry=_registry(tmp_path, journal),
        execution_journal=journal,
        approval_handler=lambda *_: True,
    )
    with pytest.raises(ExecutionJournalError, match="写入失败"):
        list(agent.stream_chat("write"))
    assert not (tmp_path / "note.txt").exists()
    assert journal.records()[0].operations[0].status == "approved"
    assert not list(journal.root.glob("*.tmp"))


def test_recovery_facts_obey_request_budget(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path)
    pending = journal.begin()
    index = journal.request(pending, "write_file", requires_approval=True)
    journal.operation(pending, index, "started")
    model = ScriptModel()
    agent = Agent(
        model, execution_journal=journal, system_prompt="system", max_context_chars=200
    )
    with pytest.raises(AgentError, match="预算"):
        agent.chat("continue")
    assert model.prompts == []
    assert journal.load(pending).needs_reconciliation


@pytest.mark.parametrize("save", [False, True])
def test_noninteractive_success_settles_execution_record(
    tmp_path: Path, save: bool
) -> None:
    settings = Settings(
        _env_file=None, workspace_root=tmp_path, deepseek_api_key="test"
    )
    result = run_noninteractive(
        settings,
        "hello",
        llm=ScriptModel(["done", ModelResponse(content="done")]),
        protocol_version=2,
        permission_mode="request",
        output_format="json",
        stdout=StringIO(),
        stderr=StringIO(),
        save_session=save,
    )
    assert result == 0
    record = ExecutionJournal(tmp_path).records()[0]
    assert record.status == ("committed" if save else "acknowledged")
    assert record.session_id is not None


def test_noninteractive_save_failure_keeps_execution_record(
    tmp_path: Path, monkeypatch
) -> None:
    from neil_agent.errors import SessionError

    settings = Settings(
        _env_file=None, workspace_root=tmp_path, deepseek_api_key="test"
    )

    def fail_save(*args, **kwargs):
        raise SessionError("cannot save")

    monkeypatch.setattr(SessionStore, "save", fail_save)
    result = run_noninteractive(
        settings,
        "hello",
        llm=ScriptModel(["done", ModelResponse(content="done")]),
        protocol_version=2,
        permission_mode="request",
        output_format="json",
        stdout=StringIO(),
        stderr=StringIO(),
        save_session=True,
    )
    assert result == 1
    assert ExecutionJournal(tmp_path).records()[0].status == "save_failed"


def test_journal_rejects_symlink_directory_without_creating_external_files(
    tmp_path: Path,
) -> None:
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    try:
        (workspace / ".neil-agent").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    with pytest.raises(ExecutionJournalError, match="真实目录"):
        ExecutionJournal(workspace).begin()
    assert list(outside.iterdir()) == []


def test_execution_ids_cannot_collide_or_escape_store(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(journal_module.secrets, "token_hex", lambda _: "a" * 32)
    journal = ExecutionJournal(tmp_path)
    execution_id = journal.begin()
    with pytest.raises(ExecutionJournalError, match="ID 冲突"):
        journal.begin()
    with pytest.raises(ExecutionJournalError, match="ID 无效"):
        journal.load("../outside")
    assert journal.load(execution_id).status == "running"


def test_retention_does_not_break_later_history_settlement(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(journal_module, "MAX_EXECUTION_RECORDS", 2)
    journal = ExecutionJournal(tmp_path)
    model = ScriptModel(
        _write_round(),
        ["done", ModelResponse(content="done")],
        ["two", ModelResponse(content="two")],
        ["three", ModelResponse(content="three")],
    )
    agent = Agent(
        model,
        registry=_registry(tmp_path, journal),
        execution_journal=journal,
        approval_handler=lambda *_: True,
    )
    list(agent.stream_chat("write"))
    write_id = agent.last_execution_id
    list(agent.stream_chat("two"))
    pruned_id = agent.last_execution_id
    list(agent.stream_chat("three"))
    assert {record.execution_id for record in journal.records()} == {
        write_id,
        agent.last_execution_id,
    }
    assert pruned_id != write_id
    agent.mark_execution("committed")
    assert all(record.status == "committed" for record in journal.records())
    assert not agent._uncommitted_executions
