"""Approximate, round-safe model context budgeting."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .schemas import Message, TokenUsage, ToolDefinition, ToolResult

ContextLayerKind = Literal[
    "system",
    "tool_schemas",
    "project_instructions",
    "selected_history",
    "current_chain",
]
CONTEXT_LAYER_ORDER: tuple[ContextLayerKind, ...] = (
    "system",
    "tool_schemas",
    "project_instructions",
    "selected_history",
    "current_chain",
)
ContextCheckpointState = Literal["none", "kept", "omitted"]
ContextToolResultState = Literal["kept", "omitted"]
ContextPressureLevel = Literal["safe", "warning", "critical", "exceeded"]
ContextPressureDimension = Literal["characters", "tokens"]
MAX_CONTEXT_TOOL_FOOTPRINTS = 3
MAX_CONTEXT_WHAT_IF_CHARS = 1_000_000
CONTEXT_PRESSURE_WARNING_BASIS_POINTS = 7_500
CONTEXT_PRESSURE_CRITICAL_BASIS_POINTS = 9_000
CONTEXT_PRESSURE_LIMIT_BASIS_POINTS = 10_000


@dataclass(frozen=True, slots=True)
class ContextSelection:
    """A contiguous suffix of complete conversation rounds."""

    messages: tuple[Message, ...]
    round_count: int
    omitted_round_count: int
    message_chars: int
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class ContextStats:
    """Current stored history and the history usable by the next request."""

    budget_chars: int
    fixed_chars: int
    stored_rounds: int
    stored_messages: int
    stored_message_chars: int
    selected_rounds: int
    selected_messages: int
    selected_message_chars: int
    omitted_rounds: int
    budget_tokens: int | None
    fixed_tokens: int
    stored_message_tokens: int
    selected_message_tokens: int


@dataclass(frozen=True, slots=True)
class ContextLayerEstimate:
    """One metadata-only layer in a local context estimate."""

    kind: ContextLayerKind
    chars: int
    estimated_tokens: int
    item_count: int

    def __post_init__(self) -> None:
        if self.kind not in CONTEXT_LAYER_ORDER:
            raise ValueError(f"unknown context layer: {self.kind}")
        if self.chars < 0:
            raise ValueError("context layer chars cannot be negative")
        if self.estimated_tokens < 0:
            raise ValueError("context layer tokens cannot be negative")
        if self.item_count < 0:
            raise ValueError("context layer item count cannot be negative")


@dataclass(frozen=True, slots=True)
class ContextToolResultFootprint:
    """One large stored tool result represented without its ID or body."""

    ordinal: int
    chars: int
    estimated_tokens: int
    state: ContextToolResultState

    def __post_init__(self) -> None:
        if self.ordinal < 1:
            raise ValueError("tool result ordinal must be positive")
        if self.chars < 0 or self.estimated_tokens < 0:
            raise ValueError("tool result footprint cannot be negative")
        if self.state not in {"kept", "omitted"}:
            raise ValueError(f"unknown tool result state: {self.state}")


@dataclass(frozen=True, slots=True)
class ContextToolResultInsights:
    """Bounded aggregate and largest tool-result footprints."""

    stored_count: int = 0
    selected_count: int = 0
    largest: tuple[ContextToolResultFootprint, ...] = ()

    def __post_init__(self) -> None:
        if self.stored_count < 0 or self.selected_count < 0:
            raise ValueError("tool result counts cannot be negative")
        if self.selected_count > self.stored_count:
            raise ValueError("selected tool results cannot exceed stored results")
        if len(self.largest) > MAX_CONTEXT_TOOL_FOOTPRINTS:
            raise ValueError("too many tool result footprints")
        ordinals = tuple(item.ordinal for item in self.largest)
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("tool result footprint ordinals must be unique")
        if any(ordinal > self.stored_count for ordinal in ordinals):
            raise ValueError("tool result footprint ordinal exceeds stored results")
        canonical = tuple(
            sorted(
                self.largest,
                key=lambda item: (-item.chars, -item.estimated_tokens, item.ordinal),
            )
        )
        if self.largest != canonical:
            raise ValueError("tool result footprints must use canonical size order")


@dataclass(frozen=True, slots=True)
class ContextBudgetPressure:
    """Deterministic pressure against local character and token budgets."""

    character_basis_points: int
    token_basis_points: int | None
    character_headroom: int
    token_headroom: int | None
    limiting_dimension: ContextPressureDimension
    level: ContextPressureLevel

    def __post_init__(self) -> None:
        if self.character_basis_points < 0:
            raise ValueError("character pressure cannot be negative")
        if self.token_basis_points is not None and self.token_basis_points < 0:
            raise ValueError("token pressure cannot be negative")
        if self.character_headroom < 0:
            raise ValueError("character headroom cannot be negative")
        if self.token_headroom is not None and self.token_headroom < 0:
            raise ValueError("token headroom cannot be negative")
        if self.limiting_dimension not in {"characters", "tokens"}:
            raise ValueError(f"unknown pressure dimension: {self.limiting_dimension}")
        if (self.token_basis_points is None) != (self.token_headroom is None):
            raise ValueError("token pressure and headroom must be configured together")
        if self.limiting_dimension == "tokens" and self.token_basis_points is None:
            raise ValueError("token pressure is required for a token limit")
        if self.level not in {"safe", "warning", "critical", "exceeded"}:
            raise ValueError(f"unknown context pressure level: {self.level}")

    @property
    def limiting_basis_points(self) -> int:
        if self.limiting_dimension == "tokens":
            assert self.token_basis_points is not None
            return self.token_basis_points
        return self.character_basis_points


@dataclass(frozen=True, slots=True)
class ContextWhatIf:
    """Metadata-only projection for one synthetic next-input size."""

    additional_chars: int
    baseline_chars: int
    baseline_tokens: int
    projected_chars: int
    projected_tokens: int
    selected_rounds_before: int
    selected_rounds_after: int
    omitted_rounds_before: int
    omitted_rounds_after: int
    baseline_pressure: ContextBudgetPressure
    projected_pressure: ContextBudgetPressure
    schema_version: Literal[1] = field(default=1, init=False)

    def __post_init__(self) -> None:
        if not 1 <= self.additional_chars <= MAX_CONTEXT_WHAT_IF_CHARS:
            raise ValueError("what-if characters are outside the supported range")
        if (
            min(
                self.baseline_chars,
                self.baseline_tokens,
                self.projected_chars,
                self.projected_tokens,
                self.selected_rounds_before,
                self.selected_rounds_after,
                self.omitted_rounds_before,
                self.omitted_rounds_after,
            )
            < 0
        ):
            raise ValueError("what-if projection values cannot be negative")
        if self.selected_rounds_after > self.selected_rounds_before:
            raise ValueError("what-if selection cannot add stored history")
        if self.omitted_rounds_after < self.omitted_rounds_before:
            raise ValueError("what-if projection cannot restore omitted history")
        if (
            self.selected_rounds_before + self.omitted_rounds_before
            != self.selected_rounds_after + self.omitted_rounds_after
        ):
            raise ValueError("what-if projection must preserve stored round count")

    @property
    def newly_omitted_rounds(self) -> int:
        return self.omitted_rounds_after - self.omitted_rounds_before


@dataclass(frozen=True, slots=True)
class ContextTomography:
    """Versioned local estimate containing counts but no context text."""

    budget_chars: int
    budget_tokens: int | None
    stored_rounds: int
    selected_rounds: int
    omitted_rounds: int
    stored_history_chars: int
    stored_history_tokens: int
    layers: tuple[ContextLayerEstimate, ...]
    last_server_usage: TokenUsage | None = None
    checkpoint_state: ContextCheckpointState = "none"
    tool_results: ContextToolResultInsights = field(
        default_factory=ContextToolResultInsights
    )
    schema_version: Literal[2] = field(default=2, init=False)

    def __post_init__(self) -> None:
        if self.budget_chars < 1:
            raise ValueError("context character budget must be positive")
        if self.budget_tokens is not None and self.budget_tokens < 1:
            raise ValueError("context token budget must be positive")
        if min(self.stored_rounds, self.selected_rounds, self.omitted_rounds) < 0:
            raise ValueError("context round counts cannot be negative")
        if self.stored_history_chars < 0 or self.stored_history_tokens < 0:
            raise ValueError("stored history footprint cannot be negative")
        if self.selected_rounds + self.omitted_rounds != self.stored_rounds:
            raise ValueError("selected and omitted rounds must equal stored rounds")
        if tuple(layer.kind for layer in self.layers) != CONTEXT_LAYER_ORDER:
            raise ValueError("context layers must use the canonical order exactly once")
        selected_history = self.layer("selected_history")
        if selected_history.chars > self.stored_history_chars:
            raise ValueError("selected history chars cannot exceed stored history")
        if selected_history.estimated_tokens > self.stored_history_tokens:
            raise ValueError("selected history tokens cannot exceed stored history")
        if self.checkpoint_state not in {"none", "kept", "omitted"}:
            raise ValueError(f"unknown checkpoint state: {self.checkpoint_state}")

    @property
    def estimated_chars(self) -> int:
        return sum(layer.chars for layer in self.layers)

    @property
    def estimated_tokens(self) -> int:
        return sum(layer.estimated_tokens for layer in self.layers)

    @property
    def omitted_history_chars(self) -> int:
        return self.stored_history_chars - self.layer("selected_history").chars

    @property
    def omitted_history_tokens(self) -> int:
        return (
            self.stored_history_tokens - self.layer("selected_history").estimated_tokens
        )

    def layer(self, kind: ContextLayerKind) -> ContextLayerEstimate:
        return self.layers[CONTEXT_LAYER_ORDER.index(kind)]


@dataclass(frozen=True, slots=True)
class PreparedCompaction:
    """Validated replacement history that has not been applied yet."""

    original_messages: tuple[Message, ...] = field(repr=False)
    compacted_messages: tuple[Message, ...] = field(repr=False)
    summarized_rounds: int
    kept_rounds: int
    old_message_chars: int
    new_message_chars: int
    summary_chars: int
    model_requests: int


def estimate_message_chars(message: Message) -> int:
    """Estimate request size from the compact API JSON representation."""

    return _json_chars(message.to_api_dict())


def estimate_message_tokens(message: Message) -> int:
    """Return a conservative model-independent token estimate for one message."""

    return estimate_text_tokens(_json_text(message.to_api_dict()))


def estimate_messages_chars(messages: Sequence[Message]) -> int:
    """Return an additive estimate suitable for per-round budgeting."""

    return sum(estimate_message_chars(message) for message in messages)


def estimate_messages_tokens(messages: Sequence[Message]) -> int:
    """Return an additive token estimate suitable for complete-round selection."""

    return sum(estimate_message_tokens(message) for message in messages)


def estimate_tool_result_chars(result: ToolResult) -> int:
    """Estimate one tool-result block without retaining its body."""

    return _json_chars(result.to_api_dict())


def estimate_tool_result_tokens(result: ToolResult) -> int:
    """Estimate tokens in one tool-result block without retaining its body."""

    return estimate_text_tokens(_json_text(result.to_api_dict()))


def estimate_fixed_chars(
    system_prompt: str,
    tools: Sequence[ToolDefinition],
) -> int:
    """Estimate the fixed system-prompt and tool-definition request cost."""

    payload = {
        "system": system_prompt,
        "tools": [definition.to_api_dict() for definition in tools],
    }
    return _json_chars(payload)


def estimate_fixed_tokens(
    system_prompt: str,
    tools: Sequence[ToolDefinition],
) -> int:
    """Estimate tokens used by the fixed prompt and tool definitions."""

    payload = {
        "system": system_prompt,
        "tools": [definition.to_api_dict() for definition in tools],
    }
    return estimate_text_tokens(_json_text(payload))


def build_context_tomography(
    *,
    system_prompt_without_project: str,
    system_prompt: str,
    has_project_instructions: bool,
    tools: Sequence[ToolDefinition],
    selected_history: ContextSelection,
    current_chain: Sequence[Message],
    stored_history: Sequence[Message],
    stored_rounds: int,
    budget_chars: int,
    budget_tokens: int | None,
    last_server_usage: TokenUsage | None,
    checkpoint_state: ContextCheckpointState,
) -> ContextTomography:
    """Split one local request estimate without retaining any source text."""

    if count_rounds(stored_history) != stored_rounds:
        raise ValueError("stored round count does not match stored history")
    system_chars = estimate_fixed_chars(system_prompt_without_project, ())
    prompt_chars = estimate_fixed_chars(system_prompt, ())
    fixed_chars = estimate_fixed_chars(system_prompt, tools)
    system_tokens = estimate_fixed_tokens(system_prompt_without_project, ())
    prompt_tokens = estimate_fixed_tokens(system_prompt, ())
    fixed_tokens = estimate_fixed_tokens(system_prompt, tools)
    current_chars = estimate_messages_chars(current_chain)
    current_tokens = estimate_messages_tokens(current_chain)
    return ContextTomography(
        budget_chars=budget_chars,
        budget_tokens=budget_tokens,
        stored_rounds=stored_rounds,
        selected_rounds=selected_history.round_count,
        omitted_rounds=selected_history.omitted_round_count,
        stored_history_chars=estimate_messages_chars(stored_history),
        stored_history_tokens=estimate_messages_tokens(stored_history),
        layers=(
            ContextLayerEstimate(
                kind="system",
                chars=system_chars,
                estimated_tokens=system_tokens,
                item_count=1,
            ),
            ContextLayerEstimate(
                kind="tool_schemas",
                chars=fixed_chars - prompt_chars,
                estimated_tokens=fixed_tokens - prompt_tokens,
                item_count=len(tools),
            ),
            ContextLayerEstimate(
                kind="project_instructions",
                chars=prompt_chars - system_chars,
                estimated_tokens=prompt_tokens - system_tokens,
                item_count=int(has_project_instructions),
            ),
            ContextLayerEstimate(
                kind="selected_history",
                chars=selected_history.message_chars,
                estimated_tokens=selected_history.estimated_tokens,
                item_count=len(selected_history.messages),
            ),
            ContextLayerEstimate(
                kind="current_chain",
                chars=current_chars,
                estimated_tokens=current_tokens,
                item_count=len(current_chain),
            ),
        ),
        last_server_usage=last_server_usage,
        checkpoint_state=checkpoint_state,
        tool_results=_build_tool_result_insights(
            stored_history,
            selected_history.messages,
        ),
    )


def context_budget_pressure(context: ContextTomography) -> ContextBudgetPressure:
    """Classify local soft-budget pressure without using server usage."""

    character_basis_points = _budget_basis_points(
        context.estimated_chars,
        context.budget_chars,
    )
    token_basis_points = (
        None
        if context.budget_tokens is None
        else _budget_basis_points(context.estimated_tokens, context.budget_tokens)
    )
    limiting_dimension: ContextPressureDimension = "characters"
    limiting_basis_points = character_basis_points
    if token_basis_points is not None and token_basis_points > character_basis_points:
        limiting_dimension = "tokens"
        limiting_basis_points = token_basis_points
    if limiting_basis_points > CONTEXT_PRESSURE_LIMIT_BASIS_POINTS:
        level: ContextPressureLevel = "exceeded"
    elif limiting_basis_points >= CONTEXT_PRESSURE_CRITICAL_BASIS_POINTS:
        level = "critical"
    elif limiting_basis_points >= CONTEXT_PRESSURE_WARNING_BASIS_POINTS:
        level = "warning"
    else:
        level = "safe"
    return ContextBudgetPressure(
        character_basis_points=character_basis_points,
        token_basis_points=token_basis_points,
        character_headroom=max(context.budget_chars - context.estimated_chars, 0),
        token_headroom=(
            None
            if context.budget_tokens is None
            else max(context.budget_tokens - context.estimated_tokens, 0)
        ),
        limiting_dimension=limiting_dimension,
        level=level,
    )


def build_context_what_if(
    baseline: ContextTomography,
    projected: ContextTomography,
    *,
    additional_chars: int,
) -> ContextWhatIf:
    """Compare an idle snapshot with a synthetic ASCII next input."""

    if not 1 <= additional_chars <= MAX_CONTEXT_WHAT_IF_CHARS:
        raise ValueError("what-if characters are outside the supported range")
    if baseline.layer("current_chain").item_count:
        raise ValueError("what-if baseline must not contain a current input")
    if projected.layer("current_chain").item_count != 1:
        raise ValueError("what-if projection must contain one synthetic input")
    if (
        baseline.budget_chars != projected.budget_chars
        or baseline.budget_tokens != projected.budget_tokens
    ):
        raise ValueError("what-if projection must use the same budgets")
    if (
        baseline.stored_rounds != projected.stored_rounds
        or baseline.stored_history_chars != projected.stored_history_chars
        or baseline.stored_history_tokens != projected.stored_history_tokens
    ):
        raise ValueError("what-if projection must use the same stored history")
    return ContextWhatIf(
        additional_chars=additional_chars,
        baseline_chars=baseline.estimated_chars,
        baseline_tokens=baseline.estimated_tokens,
        projected_chars=projected.estimated_chars,
        projected_tokens=projected.estimated_tokens,
        selected_rounds_before=baseline.selected_rounds,
        selected_rounds_after=projected.selected_rounds,
        omitted_rounds_before=baseline.omitted_rounds,
        omitted_rounds_after=projected.omitted_rounds,
        baseline_pressure=context_budget_pressure(baseline),
        projected_pressure=context_budget_pressure(projected),
    )


def _build_tool_result_insights(
    stored_history: Sequence[Message],
    selected_history: Sequence[Message],
) -> ContextToolResultInsights:
    selected_results = {
        id(result) for message in selected_history for result in message.tool_results
    }
    selected_count = sum(len(message.tool_results) for message in selected_history)
    footprints: list[ContextToolResultFootprint] = []
    ordinal = 0
    for message in stored_history:
        for result in message.tool_results:
            ordinal += 1
            footprints.append(
                ContextToolResultFootprint(
                    ordinal=ordinal,
                    chars=estimate_tool_result_chars(result),
                    estimated_tokens=estimate_tool_result_tokens(result),
                    state="kept" if id(result) in selected_results else "omitted",
                )
            )
    kept_count = sum(footprint.state == "kept" for footprint in footprints)
    if kept_count != selected_count:
        raise ValueError("selected history must reference stored history")
    largest = tuple(
        sorted(
            footprints,
            key=lambda item: (-item.chars, -item.estimated_tokens, item.ordinal),
        )[:MAX_CONTEXT_TOOL_FOOTPRINTS]
    )
    return ContextToolResultInsights(
        stored_count=ordinal,
        selected_count=selected_count,
        largest=largest,
    )


def estimate_text_tokens(text: str) -> int:
    """Estimate DeepSeek tokens without downloading a tokenizer bundle.

    DeepSeek documents approximate ratios of 0.3 token per English character
    and 0.6 token per Chinese character. Other non-ASCII characters count as
    one token. This remains a soft-budget estimate; response ``usage`` is the
    authority for actual processing and billing.
    """

    if not text:
        return 0
    ascii_count = 0
    chinese_count = 0
    other_count = 0
    for character in text:
        codepoint = ord(character)
        if codepoint < 128:
            ascii_count += 1
        elif _is_chinese_character(codepoint):
            chinese_count += 1
        else:
            other_count += 1
    return math.ceil(ascii_count * 0.3 + chinese_count * 0.6 + other_count)


def _is_chinese_character(codepoint: int) -> bool:
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
    )


def count_rounds(messages: Sequence[Message]) -> int:
    """Count top-level user requests in a complete message history."""

    return sum(
        message.role == "user" and not message.tool_results for message in messages
    )


def select_recent_rounds(
    messages: Sequence[Message],
    *,
    max_rounds: int,
    max_chars: int,
    max_tokens: int | None = None,
) -> ContextSelection:
    """Select the newest contiguous complete rounds within both limits.

    The caller supplies already validated complete history. Selection stops at
    the first round that does not fit so that older context is never retained
    while a newer round is silently skipped.
    """

    if max_rounds < 0:
        raise ValueError("max_rounds cannot be negative")
    if max_chars < 0:
        raise ValueError("max_chars cannot be negative")
    if max_tokens is not None and max_tokens < 0:
        raise ValueError("max_tokens cannot be negative")

    rounds = split_rounds(messages)
    selected_reversed: list[tuple[Message, ...]] = []
    selected_chars = 0
    selected_tokens = 0
    for conversation_round in reversed(rounds[-max_rounds:] if max_rounds else []):
        round_chars = estimate_messages_chars(conversation_round)
        round_tokens = estimate_messages_tokens(conversation_round)
        exceeds_token_budget = (
            max_tokens is not None and selected_tokens + round_tokens > max_tokens
        )
        if selected_chars + round_chars > max_chars or exceeds_token_budget:
            break
        selected_reversed.append(conversation_round)
        selected_chars += round_chars
        selected_tokens += round_tokens

    selected_rounds = tuple(reversed(selected_reversed))
    selected_messages = tuple(
        message
        for conversation_round in selected_rounds
        for message in conversation_round
    )
    return ContextSelection(
        messages=selected_messages,
        round_count=len(selected_rounds),
        omitted_round_count=len(rounds) - len(selected_rounds),
        message_chars=selected_chars,
        estimated_tokens=selected_tokens,
    )


def split_rounds(messages: Sequence[Message]) -> tuple[tuple[Message, ...], ...]:
    """Split already validated history into complete top-level user rounds."""

    starts = [
        index
        for index, message in enumerate(messages)
        if message.role == "user" and not message.tool_results
    ]
    if not starts:
        return ()
    return tuple(
        tuple(messages[start:end])
        for start, end in zip(starts, (*starts[1:], len(messages)), strict=True)
    )


def _json_chars(value: Any) -> int:
    return len(_json_text(value))


def _budget_basis_points(value: int, budget: int) -> int:
    return (value * CONTEXT_PRESSURE_LIMIT_BASIS_POINTS + budget - 1) // budget


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
