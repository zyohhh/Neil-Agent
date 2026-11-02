"""Automatic summaries must not commit partial or failed conversation state."""

from collections.abc import Iterator, Sequence
from threading import Event

import pytest

from neil_agent.agent import AUTO_COMPACTION_CHECKPOINT_USER, Agent
from neil_agent.compaction import AutoCompactionPolicy
from neil_agent.config import Settings
from neil_agent.context import estimate_fixed_tokens, estimate_messages_tokens, split_rounds
from neil_agent.errors import AgentError, LLMError, ToolError
from neil_agent.execution_budget import execution_budget_scope
from neil_agent.host_runtime import HostMode, build_agent, build_host_runtime
from neil_agent.providers.base import ProviderId, ProviderTurnState
from neil_agent.schemas import Message, ModelResponse, ThinkingContent, TokenUsage, ToolCall, ToolDefinition, ToolResult, validate_message_history
from neil_agent.session import SessionStore
from neil_agent.turn_budget import TurnBudgetExceeded, TurnLimits
from neil_agent.tools.registry import ToolRegistry


class SummaryModel:
    def __init__(self) -> None:
        self.requests: list[tuple[bool, tuple[Message, ...]]] = []
        self.summary = "Durable goal and verified earlier results."
        self.failure = ""
        self.after_summary = lambda: None
        self.before_answer = lambda: None
        self.last_usage = TokenUsage(input_tokens=5, output_tokens=2)

    def complete(self, messages: Sequence[Message], *, system_prompt: str) -> str:
        summarizing = "Conversation compaction requirements:" in system_prompt
        self.requests.append((summarizing, tuple(messages)))
        if summarizing:
            self.after_summary()
            return self.summary
        self.before_answer()
        if self.failure == "exception":
            raise LLMError("offline failure")
        return "" if self.failure == "empty" else "done"

    def stream(self, messages: Sequence[Message], *, system_prompt: str, tools: Sequence[ToolDefinition] = ()) -> Iterator[str | ModelResponse]:
        self.requests.append((False, tuple(messages)))
        if self.failure == "exception":
            raise LLMError("offline failure")
        yield "partial"
        if self.failure == "missing":
            return
        yield ModelResponse(
            content="done", usage=self.last_usage,
            stop_reason="max_tokens" if self.failure == "truncated" else "end_turn",
        )


def history(count: int = 4, size: int = 1200) -> tuple[Message, ...]:
    return tuple(
        message for index in range(count)
        for message in (Message(role="user", content=f"goal-{index}:" + "x" * size),
                        Message(role="assistant", content=f"result-{index}:" + "y" * size))
    )


def make_agent(model: SummaryModel, **options) -> Agent:
    agent = Agent(
        model, system_prompt="test", max_context_chars=options.pop("max_context_chars", 10_000),
        auto_compaction=options.pop("auto_compaction", AutoCompactionPolicy(enabled=True)),
        **options,
    )
    agent.restore_messages(history())
    return agent


@pytest.mark.parametrize("streaming", [False, True])
def test_pressure_summary_keeps_recent_rounds_and_commits_with_success(streaming: bool) -> None:
    model = SummaryModel()
    agent = make_agent(model)
    original = agent.messages
    if streaming:
        assert "".join(agent.stream_chat("new request")) == "partial"
    else:
        assert agent.chat("new request") == "done"
    assert [summary for summary, _ in model.requests] == [True, False]
    assert "goal-0:" in model.requests[0][1][0].content
    assert "goal-2:" not in model.requests[0][1][0].content
    assert "new request" not in model.requests[0][1][0].content
    sent = model.requests[-1][1]
    assert sent[0].content == AUTO_COMPACTION_CHECKPOINT_USER
    assert tuple(sent[2:-1]) == original[-4:]
    assert agent.messages[:2] == sent[:2]
    assert agent.messages[2:-2] == original[-4:]
    validate_message_history(agent.messages)
    assert agent.last_run_budget.model_attempts == 2
    assert agent.last_run_budget.usage == TokenUsage(input_tokens=10, output_tokens=4)
    assert agent.last_run_budget.usage_complete
    assert agent.last_usage == TokenUsage(input_tokens=5, output_tokens=2)


def test_default_disabled_does_not_request_a_summary() -> None:
    model = SummaryModel()
    agent = make_agent(model, auto_compaction=AutoCompactionPolicy())
    agent.chat("new request")
    assert [summary for summary, _ in model.requests] == [False]


def test_token_pressure_triggers_with_character_space_available() -> None:
    model = SummaryModel()
    pending = (*history(), Message(role="user", content="q"))
    token_limit = int((estimate_fixed_tokens("test", ()) + estimate_messages_tokens(pending)) / 0.85)
    agent = make_agent(model, max_context_chars=100_000, max_context_tokens=token_limit)
    agent.chat("q")
    assert [summary for summary, _ in model.requests] == [True, False]


def test_round_pressure_summarizes_before_old_rounds_would_be_trimmed() -> None:
    model = SummaryModel()
    agent = make_agent(model, max_rounds=4, max_context_chars=100_000)
    agent.chat("q")
    assert len(split_rounds(agent.messages)) == 4
    assert model.requests[0][0]
    assert agent.messages[0].content == AUTO_COMPACTION_CHECKPOINT_USER


def test_recent_tool_round_and_provider_payload_remain_complete() -> None:
    model = SummaryModel()
    agent = make_agent(model)
    call = ToolCall(id="call-old", name="read_file", arguments={"path": "a.py"})
    state = ProviderTurnState(provider=ProviderId.OPENAI, model="fixture", schema_version=1, payload={"items": [{"opaque": "retained"}]})
    tool_round = (
        Message(role="user", content="recent tool request"),
        Message(role="assistant", tool_calls=(call,), provider_state=state),
        Message(role="user", tool_results=(ToolResult(tool_call_id=call.id, content="source"),)),
        Message(role="assistant", content="verified result"),
    )
    agent.restore_messages((*history(), *tool_round))
    agent.chat("q")
    assert model.requests[-1][1][-5:-1] == tool_round
    assert all(call.id not in message.content for message in model.requests[0][1])
    validate_message_history(model.requests[-1][1][:-1])
    assert model.requests[-1][1][-4].provider_state == state


def test_summary_uses_visible_history_without_private_protocol_payload() -> None:
    model = SummaryModel()
    agent = make_agent(model)
    old = list(agent.messages)
    old[1] = Message(
        role="assistant", content=old[1].content,
        thinking=(ThinkingContent(thinking="private reasoning", signature="private signature"),),
        provider_state=ProviderTurnState(
            provider=ProviderId.OPENAI, model="fixture", schema_version=1,
            payload={"private_replay": "private replay value"},
        ),
    )
    agent.restore_messages(old)
    agent.chat("q")
    transcript = model.requests[0][1][0].content
    assert "private" not in transcript
    assert old[1].content in transcript


def test_automatic_checkpoint_survives_session_save_load_and_further_turns(tmp_path) -> None:
    model = SummaryModel()
    agent = make_agent(model, max_rounds=4)
    agent.chat("first request")
    store = SessionStore(tmp_path)
    handle = store.new_session()
    store.save(handle, agent.messages, (), None, last_usage=agent.last_usage)
    loaded = store.load(handle.session_id)
    assert loaded.messages == agent.messages
    restored = make_agent(SummaryModel(), max_rounds=4)
    restored.restore_messages(loaded.messages, last_usage=loaded.last_usage)
    restored.chat("next request")
    assert restored.messages[0].content == AUTO_COMPACTION_CHECKPOINT_USER
    assert len(split_rounds(restored.messages)) == 4
    validate_message_history(restored.messages)


def test_compacted_turn_can_complete_a_real_registry_tool_round() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(name="read_number", description="Read the fixture number", input_schema={"type": "object"}),
        lambda: "42",
    )

    class ToolModel(SummaryModel):
        def stream(self, messages, *, system_prompt, tools=()):
            self.requests.append((False, tuple(messages)))
            if messages[-1].tool_results:
                assert messages[-1].tool_results[0].content == "42"
                yield ModelResponse(content="verified 42", usage=self.last_usage, stop_reason="end_turn")
            else:
                yield ModelResponse(
                    tool_calls=(ToolCall(id="call-new", name="read_number"),),
                    usage=self.last_usage, stop_reason="tool_call",
                )

    model = ToolModel()
    agent = make_agent(model, registry=registry)
    original = agent.messages
    list(agent.stream_chat("verify number"))
    validate_message_history(agent.messages)
    assert agent.messages[0].content == AUTO_COMPACTION_CHECKPOINT_USER
    assert agent.messages[2:6] == original[-4:]
    assert [summary for summary, _ in model.requests] == [True, False, False]
    assert agent.last_run_budget.model_attempts == 3
    assert agent.last_run_budget.usage == TokenUsage(input_tokens=15, output_tokens=6)
    assert agent.last_usage == TokenUsage(input_tokens=10, output_tokens=4)


def test_closing_stream_keeps_original_history_and_discards_candidate() -> None:
    model = SummaryModel()
    agent = make_agent(model)
    original = agent.messages
    stream = agent.stream_chat("q")
    assert next(stream) == "partial"
    assert agent.messages == original
    stream.close()
    assert agent.messages == original
    agent.chat("retry")
    assert [summary for summary, _ in model.requests] == [True, False, True, False]


@pytest.mark.parametrize("failure", ["exception", "missing", "truncated"])
def test_failed_stream_does_not_save_the_summary(failure: str) -> None:
    model = SummaryModel()
    model.failure = failure
    agent = make_agent(model)
    original = agent.messages
    with pytest.raises((LLMError, AgentError)):
        list(agent.stream_chat("q"))
    assert agent.messages == original


@pytest.mark.parametrize("failure", ["exception", "empty"])
def test_failed_nonstream_response_does_not_save_the_summary(failure: str) -> None:
    model = SummaryModel()
    model.failure = failure
    agent = make_agent(model)
    original = agent.messages
    with pytest.raises((LLMError, AgentError)):
        agent.chat("q")
    assert agent.messages == original


@pytest.mark.parametrize("summary", ["", "x" * 8001, "x" * 7000])
def test_bad_or_nonshrinking_summary_never_replaces_history(summary: str) -> None:
    model = SummaryModel()
    model.summary = summary
    agent = make_agent(model)
    original = agent.messages
    with pytest.raises(AgentError):
        agent.chat("q")
    assert agent.messages == original
    assert all(summarizing for summarizing, _ in model.requests)


def test_changed_history_is_not_overwritten_by_a_stale_summary() -> None:
    model = SummaryModel()
    agent = make_agent(model)
    other = history(1, 50)
    model.after_summary = lambda: agent.restore_messages(other)
    with pytest.raises(AgentError, match="历史发生变化"):
        agent.chat("q")
    assert agent.messages == other
    assert [summary for summary, _ in model.requests] == [True]


def test_history_changed_during_answer_is_checked_again_before_commit() -> None:
    model = SummaryModel()
    agent = make_agent(model)
    other = history(1, 50)
    model.before_answer = lambda: agent.restore_messages(other)
    with pytest.raises(AgentError, match="历史发生变化"):
        agent.chat("q")
    assert agent.messages == other
    assert [summary for summary, _ in model.requests] == [True, False]


def test_summary_and_answer_cannot_get_separate_attempt_allowances() -> None:
    model = SummaryModel()
    agent = make_agent(model, turn_limits=TurnLimits(max_model_attempts=1))
    original = agent.messages
    with pytest.raises(TurnBudgetExceeded):
        agent.chat("q")
    assert agent.messages == original
    assert [summary for summary, _ in model.requests] == [True]
    assert agent.last_run_budget.model_attempts == 1
    assert agent.last_run_budget.stop_reason == "model_attempts"


def test_cancellation_after_summary_keeps_history_and_skips_answer() -> None:
    model = SummaryModel()
    cancelled = Event()
    model.after_summary = cancelled.set
    agent = make_agent(model)
    original = agent.messages
    with execution_budget_scope(cancel=cancelled), pytest.raises(ToolError):
        agent.chat("q")
    assert agent.messages == original
    assert [summary for summary, _ in model.requests] == [True]


def test_oversized_current_input_does_not_spend_a_summary_request() -> None:
    model = SummaryModel()
    agent = make_agent(model)
    original = agent.messages
    with pytest.raises(AgentError, match="当前回合上下文"):
        agent.chat("q" * 11_000)
    assert agent.messages == original and model.requests == []


def test_candidate_must_fit_with_current_input_and_recent_rounds() -> None:
    model = SummaryModel()
    agent = make_agent(model)
    original = agent.messages
    with pytest.raises(AgentError, match="自动压缩后仍无法容纳"):
        agent.chat("q" * 5500)
    assert agent.messages == original
    assert [summary for summary, _ in model.requests] == [True]


def test_existing_checkpoint_alone_is_not_repeatedly_summarized() -> None:
    model = SummaryModel()
    agent = make_agent(model)
    agent.chat("q")
    agent.restore_messages(agent.messages[:-2])
    model.requests.clear()
    agent.chat("q")
    assert [summary for summary, _ in model.requests] == [False]


@pytest.mark.parametrize("max_rounds", [1, 2])
def test_tiny_round_limit_keeps_old_behavior_without_summary_calls(max_rounds: int) -> None:
    model = SummaryModel()
    agent = make_agent(model, max_rounds=max_rounds)
    agent.chat("q")
    assert [summary for summary, _ in model.requests] == [False]


@pytest.mark.parametrize("options", [
    {"auto_compact_trigger_ratio": 0}, {"auto_compact_trigger_ratio": 1.01},
    {"auto_compact_trigger_ratio": float("nan")}, {"auto_compact_trigger_ratio": float("inf")},
    {"auto_compact_keep_recent_rounds": 0},
])
def test_invalid_compaction_settings_are_rejected(options) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, deepseek_api_key="offline-fixture", **options)


@pytest.mark.parametrize("mode", list(HostMode))
def test_shared_host_builds_the_configured_policy(tmp_path, mode) -> None:
    settings = Settings(
        _env_file=None, workspace_root=tmp_path, deepseek_api_key="offline-fixture",
        auto_compact_enabled=True, auto_compact_trigger_ratio=0.7,
        auto_compact_keep_recent_rounds=1, audit_log_enabled=False,
    )
    with_runtime = build_host_runtime(settings, mode=mode)
    try:
        agent = build_agent(settings, with_runtime, SummaryModel())
        assert agent._auto_compaction == AutoCompactionPolicy(True, 0.7, 1)
    finally:
        with_runtime.close()
