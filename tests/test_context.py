"""Tests for approximate, complete-round context budgeting."""

from collections.abc import Iterator, Sequence

import pytest

from neil_agent.agent import COMPACTION_CHECKPOINT_USER, Agent
from neil_agent.context import (
    CONTEXT_LAYER_ORDER,
    MAX_CONTEXT_WHAT_IF_CHARS,
    ContextLayerEstimate,
    ContextTomography,
    ContextToolResultFootprint,
    ContextToolResultInsights,
    build_context_tomography,
    context_budget_pressure,
    estimate_fixed_chars,
    estimate_fixed_tokens,
    estimate_message_chars,
    estimate_message_tokens,
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
    TokenUsage,
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
    tomography = agent.context_tomography("next")
    agent.chat("next")

    assert stats.stored_rounds == 2
    assert stats.selected_rounds == 1
    assert stats.omitted_rounds == 1
    assert [message.content for message in model.requests[-1]] == [
        "recent",
        "small",
        "next",
    ]
    assert tomography.schema_version == 2
    assert tomography.selected_rounds == 1
    assert tomography.omitted_rounds == 1
    assert tomography.layer("selected_history").item_count == 2
    assert tomography.layer("current_chain").item_count == 1


def test_empty_context_has_fixed_cost_but_no_history() -> None:
    agent = Agent(ContextFakeModel(), system_prompt="short")
    stats = agent.context_stats()
    tomography = agent.context_tomography()

    assert stats.fixed_chars > 0
    assert stats.stored_rounds == 0
    assert stats.selected_messages == 0
    assert tomography.budget_tokens is None
    assert tomography.layer("tool_schemas").chars == 0
    assert tomography.layer("project_instructions").chars == 0
    assert tomography.layer("current_chain").item_count == 0


def test_context_tomography_is_additive_and_retains_no_source_text() -> None:
    system = "SYSTEM-CANARY"
    project = "PROJECT-CANARY"
    full_system = f"{system}\n\n{project}"
    tools = (
        ToolDefinition(
            name="inspect_value",
            description="TOOL-CANARY",
            input_schema={"type": "object", "properties": {}},
        ),
    )
    history = (
        Message(role="user", content="HISTORY-CANARY"),
        Message(role="assistant", content="answer"),
    )
    selected = select_recent_rounds(
        history,
        max_rounds=1,
        max_chars=10_000,
    )
    current = (Message(role="user", content="CURRENT-CANARY"),)

    tomography = build_context_tomography(
        system_prompt_without_project=system,
        system_prompt=full_system,
        has_project_instructions=True,
        tools=tools,
        selected_history=selected,
        current_chain=current,
        stored_history=history,
        stored_rounds=1,
        budget_chars=20_000,
        budget_tokens=8_000,
        last_server_usage=None,
        checkpoint_state="none",
    )

    assert tuple(layer.kind for layer in tomography.layers) == CONTEXT_LAYER_ORDER
    assert tomography.estimated_chars == (
        estimate_fixed_chars(full_system, tools)
        + selected.message_chars
        + estimate_message_chars(current[0])
    )
    assert tomography.estimated_tokens == (
        estimate_fixed_tokens(full_system, tools)
        + selected.estimated_tokens
        + estimate_message_tokens(current[0])
    )
    assert tomography.layer("tool_schemas").item_count == 1
    assert tomography.layer("project_instructions").item_count == 1
    rendered_snapshot = repr(tomography)
    for secret in (
        "SYSTEM-CANARY",
        "PROJECT-CANARY",
        "TOOL-CANARY",
        "HISTORY-CANARY",
        "CURRENT-CANARY",
    ):
        assert secret not in rendered_snapshot


def test_context_tomography_rejects_invalid_rounds_and_layer_order() -> None:
    layers = tuple(ContextLayerEstimate(kind, 0, 0, 0) for kind in CONTEXT_LAYER_ORDER)

    with pytest.raises(ValueError, match="equal stored rounds"):
        ContextTomography(
            budget_chars=1,
            budget_tokens=None,
            stored_rounds=2,
            selected_rounds=1,
            omitted_rounds=0,
            stored_history_chars=0,
            stored_history_tokens=0,
            layers=layers,
        )

    with pytest.raises(ValueError, match="canonical order"):
        ContextTomography(
            budget_chars=1,
            budget_tokens=None,
            stored_rounds=0,
            selected_rounds=0,
            omitted_rounds=0,
            stored_history_chars=0,
            stored_history_tokens=0,
            layers=tuple(reversed(layers)),
        )

    with pytest.raises(ValueError, match="cannot be negative"):
        ContextLayerEstimate("system", -1, 0, 0)

    with pytest.raises(ValueError, match="cannot exceed stored"):
        ContextToolResultInsights(stored_count=1, selected_count=2)

    with pytest.raises(ValueError, match="canonical size order"):
        ContextToolResultInsights(
            stored_count=2,
            selected_count=1,
            largest=(
                ContextToolResultFootprint(1, 10, 3, "kept"),
                ContextToolResultFootprint(2, 20, 6, "omitted"),
            ),
        )


def test_context_tomography_normalizes_base_prompt_before_project_delta() -> None:
    agent = Agent(
        ContextFakeModel(),
        system_prompt="system" + " " * 100,
        project_instructions="project rule",
    )

    tomography = agent.context_tomography()

    assert tomography.layer("project_instructions").chars > 0
    assert tomography.layer("project_instructions").estimated_tokens > 0
    assert tomography.estimated_chars == agent.context_stats().fixed_chars
    assert tomography.estimated_tokens == agent.context_stats().fixed_tokens


def test_context_tomography_reports_historical_usage_and_tool_footprints() -> None:
    stored_history = (
        Message(role="user", content="old"),
        Message(
            role="assistant",
            tool_calls=(ToolCall(id="old-secret-id", name="read_file"),),
        ),
        Message(
            role="user",
            tool_results=(
                ToolResult(
                    tool_call_id="old-secret-id",
                    content="OMITTED-RESULT-CANARY" * 200,
                ),
            ),
        ),
        Message(role="assistant", content="old done"),
        Message(role="user", content="recent"),
        Message(
            role="assistant",
            tool_calls=(ToolCall(id="recent-secret-id", name="read_file"),),
        ),
        Message(
            role="user",
            tool_results=(
                ToolResult(
                    tool_call_id="recent-secret-id",
                    content="KEPT-RESULT-CANARY" * 20,
                ),
            ),
        ),
        Message(role="assistant", content="recent done"),
    )
    selected = select_recent_rounds(
        stored_history,
        max_rounds=1,
        max_chars=100_000,
    )
    usage = TokenUsage(
        input_tokens=1_200,
        output_tokens=80,
        cache_creation_input_tokens=300,
        cache_read_input_tokens=700,
    )

    tomography = build_context_tomography(
        system_prompt_without_project="system",
        system_prompt="system",
        has_project_instructions=False,
        tools=(),
        selected_history=selected,
        current_chain=(),
        stored_history=stored_history,
        stored_rounds=2,
        budget_chars=100_000,
        budget_tokens=50_000,
        last_server_usage=usage,
        checkpoint_state="none",
    )

    assert tomography.last_server_usage == usage
    assert tomography.omitted_rounds == 1
    assert tomography.omitted_history_chars > 0
    assert tomography.omitted_history_tokens > 0
    assert tomography.tool_results.stored_count == 2
    assert tomography.tool_results.selected_count == 1
    assert [item.ordinal for item in tomography.tool_results.largest] == [1, 2]
    assert [item.state for item in tomography.tool_results.largest] == [
        "omitted",
        "kept",
    ]
    snapshot_text = repr(tomography)
    for canary in (
        "OMITTED-RESULT-CANARY",
        "KEPT-RESULT-CANARY",
        "old-secret-id",
        "recent-secret-id",
    ):
        assert canary not in snapshot_text


def test_context_tomography_marks_compaction_checkpoint_selection() -> None:
    agent = Agent(
        ContextFakeModel(),
        max_context_chars=20_000,
    )
    agent.restore_messages(
        (
            Message(role="user", content=COMPACTION_CHECKPOINT_USER),
            Message(
                role="assistant",
                content="[Compressed conversation summary]\ndurable facts",
            ),
            Message(role="user", content="recent"),
            Message(role="assistant", content="reply"),
        ),
        last_usage=TokenUsage(input_tokens=90, output_tokens=10),
    )

    kept = agent.context_tomography()
    omitted = agent.context_tomography("x" * 30_000)

    assert kept.checkpoint_state == "kept"
    assert kept.last_server_usage == TokenUsage(input_tokens=90, output_tokens=10)
    assert omitted.checkpoint_state == "omitted"
    assert omitted.selected_rounds == 0


@pytest.mark.parametrize(
    ("estimated_chars", "expected_level"),
    (
        (7_499, "safe"),
        (7_500, "warning"),
        (9_000, "critical"),
        (10_000, "critical"),
        (10_001, "exceeded"),
    ),
)
def test_context_pressure_uses_deterministic_soft_budget_thresholds(
    estimated_chars: int,
    expected_level: str,
) -> None:
    layers = (
        ContextLayerEstimate("system", estimated_chars, 100, 1),
        ContextLayerEstimate("tool_schemas", 0, 0, 0),
        ContextLayerEstimate("project_instructions", 0, 0, 0),
        ContextLayerEstimate("selected_history", 0, 0, 0),
        ContextLayerEstimate("current_chain", 0, 0, 0),
    )
    tomography = ContextTomography(
        budget_chars=10_000,
        budget_tokens=None,
        stored_rounds=0,
        selected_rounds=0,
        omitted_rounds=0,
        stored_history_chars=0,
        stored_history_tokens=0,
        layers=layers,
    )

    pressure = context_budget_pressure(tomography)

    assert pressure.level == expected_level
    assert pressure.limiting_dimension == "characters"
    assert pressure.character_headroom == max(10_000 - estimated_chars, 0)


def test_context_pressure_uses_optional_token_budget_when_more_constrained() -> None:
    tomography = ContextTomography(
        budget_chars=10_000,
        budget_tokens=1_000,
        stored_rounds=0,
        selected_rounds=0,
        omitted_rounds=0,
        stored_history_chars=0,
        stored_history_tokens=0,
        layers=(
            ContextLayerEstimate("system", 5_000, 950, 1),
            ContextLayerEstimate("tool_schemas", 0, 0, 0),
            ContextLayerEstimate("project_instructions", 0, 0, 0),
            ContextLayerEstimate("selected_history", 0, 0, 0),
            ContextLayerEstimate("current_chain", 0, 0, 0),
        ),
    )

    pressure = context_budget_pressure(tomography)

    assert pressure.level == "critical"
    assert pressure.limiting_dimension == "tokens"
    assert pressure.token_basis_points == 9_500
    assert pressure.token_headroom == 50


def test_agent_context_what_if_is_local_bounded_and_round_safe() -> None:
    model = ContextFakeModel()
    agent = Agent(
        model,
        system_prompt="s",
        max_rounds=4,
        max_context_chars=600,
    )
    agent.restore_messages(
        (
            Message(role="user", content="recent"),
            Message(role="assistant", content="history" * 25),
        ),
        last_usage=TokenUsage(input_tokens=90, output_tokens=10),
    )
    original_messages = agent.messages
    original_usage = agent.last_usage

    simulation = agent.context_what_if(550)

    assert simulation.schema_version == 1
    assert simulation.additional_chars == 550
    assert simulation.projected_pressure.level == "exceeded"
    assert simulation.selected_rounds_before == 1
    assert simulation.selected_rounds_after == 0
    assert simulation.newly_omitted_rounds == 1
    assert agent.messages == original_messages
    assert agent.last_usage == original_usage
    assert model.requests == []
    assert "xxxxxxxxxxxxxxxxxxxx" not in repr(simulation)

    with pytest.raises(ValueError, match="supported range"):
        agent.context_what_if(0)
    with pytest.raises(ValueError, match="supported range"):
        agent.context_what_if(MAX_CONTEXT_WHAT_IF_CHARS + 1)


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
