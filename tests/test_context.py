"""Tests for approximate, complete-round context budgeting."""

from collections.abc import Iterator, Sequence

from neil_agent.agent import Agent
from neil_agent.context import (
    estimate_messages_chars,
    estimate_messages_tokens,
    estimate_text_tokens,
    select_recent_rounds,
)
from neil_agent.schemas import (
    Message,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    ToolResult,
)


class ContextFakeModel:
    def __init__(self) -> None:
        self.requests: list[list[Message]] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
    ) -> str:
        self.requests.append(list(messages))
        return "reply"

    def stream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> Iterator[str | ModelResponse]:
        raise NotImplementedError


def test_context_budget_keeps_a_tool_round_whole() -> None:
    messages = (
        Message(role="user", content="inspect"),
        Message(
            role="assistant",
            tool_calls=(ToolCall(id="call-1", name="read_file"),),
        ),
        Message(
            role="user",
            tool_results=(ToolResult(tool_call_id="call-1", content="x" * 2_000),),
        ),
        Message(role="assistant", content="done"),
    )
    round_chars = estimate_messages_chars(messages)

    excluded = select_recent_rounds(
        messages,
        max_rounds=1,
        max_chars=round_chars - 1,
    )
    included = select_recent_rounds(
        messages,
        max_rounds=1,
        max_chars=round_chars,
    )

    assert excluded.messages == ()
    assert excluded.omitted_round_count == 1
    assert included.messages == messages
    assert included.round_count == 1


def test_context_budget_does_not_skip_a_newer_oversized_round() -> None:
    old_round = (
        Message(role="user", content="old"),
        Message(role="assistant", content="small"),
    )
    newest_round = (
        Message(role="user", content="new"),
        Message(role="assistant", content="x" * 2_000),
    )

    selection = select_recent_rounds(
        (*old_round, *newest_round),
        max_rounds=2,
        max_chars=estimate_messages_chars(old_round),
    )

    assert selection.messages == ()
    assert selection.omitted_round_count == 2


def test_agent_reports_and_applies_context_budget_to_previous_rounds() -> None:
    model = ContextFakeModel()
    agent = Agent(
        model,
        system_prompt="s",
        max_rounds=4,
        max_context_chars=400,
    )
    agent.restore_messages(
        (
            Message(role="user", content="old"),
            Message(role="assistant", content="x" * 600),
            Message(role="user", content="recent"),
            Message(role="assistant", content="small"),
        )
    )

    stats = agent.context_stats()
    agent.chat("next")

    assert stats.stored_rounds == 2
    assert stats.selected_rounds == 1
    assert stats.omitted_rounds == 1
    assert [message.content for message in model.requests[-1]] == [
        "recent",
        "small",
        "next",
    ]


def test_empty_context_has_fixed_cost_but_no_history() -> None:
    stats = Agent(ContextFakeModel(), system_prompt="short").context_stats()

    assert stats.fixed_chars > 0
    assert stats.stored_rounds == 0
    assert stats.selected_messages == 0


def test_optional_token_budget_can_be_stricter_than_character_budget() -> None:
    messages = (
        Message(role="user", content="你好" * 100),
        Message(role="assistant", content="完成"),
    )
    estimated_tokens = estimate_messages_tokens(messages)

    excluded = select_recent_rounds(
        messages,
        max_rounds=1,
        max_chars=10_000,
        max_tokens=estimated_tokens - 1,
    )
    included = select_recent_rounds(
        messages,
        max_rounds=1,
        max_chars=10_000,
        max_tokens=estimated_tokens,
    )

    assert excluded.messages == ()
    assert included.messages == messages
    assert included.estimated_tokens == estimated_tokens


def test_agent_reports_configured_token_soft_budget() -> None:
    agent = Agent(
        ContextFakeModel(),
        system_prompt="short",
        max_context_tokens=2_000,
    )

    stats = agent.context_stats()

    assert stats.budget_tokens == 2_000
    assert stats.fixed_tokens > 0


def test_text_estimate_uses_documented_deepseek_character_ratios() -> None:
    assert estimate_text_tokens("a" * 10) == 3
    assert estimate_text_tokens("中" * 10) == 6
    assert estimate_text_tokens("a中") == 1
