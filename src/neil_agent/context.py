"""Approximate, round-safe model context budgeting."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .schemas import Message, ToolDefinition


@dataclass(frozen=True, slots=True)
class ContextSelection:
    """A contiguous suffix of complete conversation rounds."""

    messages: tuple[Message, ...]
    round_count: int
    omitted_round_count: int
    message_chars: int


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


def estimate_message_chars(message: Message) -> int:
    """Estimate request size from the compact API JSON representation."""

    return _json_chars(message.to_api_dict())


def estimate_messages_chars(messages: Sequence[Message]) -> int:
    """Return an additive estimate suitable for per-round budgeting."""

    return sum(estimate_message_chars(message) for message in messages)


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

    rounds = _split_rounds(messages)
    selected_reversed: list[tuple[Message, ...]] = []
    selected_chars = 0
    for conversation_round in reversed(rounds[-max_rounds:] if max_rounds else []):
        round_chars = estimate_messages_chars(conversation_round)
        if selected_chars + round_chars > max_chars:
            break
        selected_reversed.append(conversation_round)
        selected_chars += round_chars

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
    )


def _split_rounds(messages: Sequence[Message]) -> tuple[tuple[Message, ...], ...]:
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
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
