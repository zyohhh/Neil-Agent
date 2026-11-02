"""Bounded metadata-only detection of stable tool loops and failed rounds."""

from __future__ import annotations

from hashlib import sha256
import json

from .errors import AgentError
from .schemas import ToolCall, ToolResult


class AgentLoopDetected(AgentError):
    pass


class ToolLoopGuard:
    def __init__(self, repeated_result_limit: int = 3, failed_round_limit: int = 3) -> None:
        if repeated_result_limit < 2 or failed_round_limit < 1:
            raise ValueError("invalid loop guard limits")
        self._repeated_limit = repeated_result_limit
        self._failed_limit = failed_round_limit
        self._observations: dict[str, tuple[str, int]] = {}
        self._failed_rounds = 0
        self._instruction_digest: str | None = None

    def check_round(
        self, pairs: list[tuple[ToolCall, ToolResult]], *,
        instruction_digest: str, approved_tools: set[str],
    ) -> None:
        if instruction_digest != self._instruction_digest:
            self._observations.clear()
            self._failed_rounds = 0
            self._instruction_digest = instruction_digest
        successful = False
        fresh = False
        max_repeat = 0
        batch_counts: dict[tuple[str, str], int] = {}
        for call, result in pairs:
            try:
                arguments = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise AgentLoopDetected("工具参数无法规范化，已停止当前回合。") from error
            key = sha256((call.name + "\0" + arguments).encode()).hexdigest()
            digest = sha256((str(result.is_error) + "\0" + result.content).encode()).hexdigest()
            previous = self._observations.get(key)
            changed = previous is None or previous[0] != digest
            if changed and not result.is_error and call.name in approved_tools:
                # A new approved operation may change what an earlier read means.
                self._observations.clear()
            if key not in self._observations and len(self._observations) >= 256:
                raise AgentLoopDetected("工具观察记录达到上限，已停止当前回合。")
            count = 1 if previous is None or changed else previous[1] + 1
            self._observations[key] = (digest, count)
            max_repeat = max(max_repeat, count)
            batch_key = (key, digest)
            if batch_key not in batch_counts and len(batch_counts) >= 256:
                raise AgentLoopDetected("单批工具观察记录达到上限，已停止当前回合。")
            batch_counts[batch_key] = batch_counts.get(batch_key, 0) + 1
            if not result.is_error:
                successful = True
                fresh = fresh or changed
        self._failed_rounds = 0 if successful else self._failed_rounds + 1
        if self._failed_rounds >= self._failed_limit:
            raise AgentLoopDetected("连续多轮工具调用均失败或被拒绝，已停止当前任务。")
        if max(batch_counts.values(), default=0) >= self._repeated_limit or not fresh and max_repeat >= self._repeated_limit:
            raise AgentLoopDetected("工具调用重复返回相同结果且没有新增成功观察，已停止当前任务。")
        if fresh:
            self._observations = {key: (digest, 1) for key, (digest, _) in self._observations.items()}
