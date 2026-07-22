"""Approximate, round-safe model context budgeting."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .schemas import Message, ToolDefinition


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


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens without a model tokenizer.

    ASCII content is approximated at four characters per token. Non-ASCII
    characters count as one token each, which is intentionally conservative
    for common CJK project text. This is a soft-budget fallback, not billing
    data or an exact DeepSeek tokenizer result.
    """

    if not text:
        return 0
    ascii_count = sum(ord(character) < 128 for character in text)
    non_ascii_count = len(text) - ascii_count
    return math.ceil(ascii_count / 4) + non_ascii_count


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
            max_tokens is not None
            and selected_tokens + round_tokens > max_tokens
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


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
