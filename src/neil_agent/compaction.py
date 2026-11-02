"""Decide when complete older rounds need an opt-in summary checkpoint."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

from .context import estimate_fixed_chars, estimate_fixed_tokens, estimate_messages_chars, estimate_messages_tokens, split_rounds
from .schemas import Message, ToolDefinition


@dataclass(frozen=True, slots=True)
class AutoCompactionPolicy:
    enabled: bool = False
    trigger_ratio: float = 0.8
    keep_recent_rounds: int = 2

    def __post_init__(self) -> None:
        if not isfinite(self.trigger_ratio) or not 0 < self.trigger_ratio <= 1:
            raise ValueError("compaction trigger ratio must be in (0, 1]")
        if self.keep_recent_rounds < 1:
            raise ValueError("compaction must retain at least one complete round")

    def plan(
        self, history: Sequence[Message], current: Message, *,
        system_prompt: str, tools: Sequence[ToolDefinition],
        max_rounds: int, max_chars: int, max_tokens: int | None,
        has_checkpoint: bool,
    ) -> int | None:
        if not self.enabled or max_rounds < 3:
            return None
        keep = min(self.keep_recent_rounds, max_rounds - 2)
        rounds = split_rounds(history)
        # Re-summarizing only the existing checkpoint cannot recover more history.
        if len(rounds) <= keep + int(has_checkpoint):
            return None
        pending = (*history, current)
        char_pressure = estimate_fixed_chars(system_prompt, tools) + estimate_messages_chars(pending)
        token_pressure = (
            max_tokens is not None
            and estimate_fixed_tokens(system_prompt, tools) + estimate_messages_tokens(pending)
            >= max_tokens * self.trigger_ratio
        )
        if len(rounds) >= max_rounds or char_pressure >= max_chars * self.trigger_ratio or token_pressure:
            return keep
        return None
