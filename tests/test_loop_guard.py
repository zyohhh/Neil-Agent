"""Stable repeats stop spending; fresh results and valid follow-up work continue."""

import pytest

from neil_agent.agent import Agent
from neil_agent.loop_guard import AgentLoopDetected, ToolLoopGuard
from neil_agent.schemas import ModelResponse, ToolCall, ToolDefinition, ToolResult
from neil_agent.tools.registry import ToolRegistry


def pair(value="same", *, name="read", query="a", failed=False, number=0):
    call = ToolCall(id=str(number), name=name, arguments={"query": query})
    return call, ToolResult(tool_call_id=call.id, content=value, is_error=failed)


def test_ids_and_assistant_rephrasing_do_not_hide_identical_observations():
    registry = ToolRegistry()
    registry.register(ToolDefinition(name="read", description="fixture", input_schema={}), lambda query: "same")

    class Model:
        calls = 0

        def stream(self, *args, **kwargs):
            self.calls += 1
            yield ModelResponse(content=f"different wording {self.calls}", tool_calls=(pair(number=self.calls)[0],))

    model = Model()
    agent = Agent(model, registry=registry, max_tool_rounds=20)
    with pytest.raises(AgentLoopDetected, match="相同结果"):
        list(agent.stream_chat("request"))
    assert model.calls == 3 and not agent.messages
    assert agent.last_run_budget.model_attempts == 3


def test_a_b_read_cycle_is_detected():
    guard = ToolLoopGuard()
    for number, query in enumerate(["a", "b", "a", "b"]):
        guard.check_round([pair(query=query, number=number)], instruction_digest="v1", approved_tools=set())
    with pytest.raises(AgentLoopDetected):
        guard.check_round([pair(query="a", number=5)], instruction_digest="v1", approved_tools=set())


def test_identical_observations_in_one_batch_are_also_detected():
    with pytest.raises(AgentLoopDetected):
        ToolLoopGuard().check_round([pair(number=number) for number in range(3)], instruction_digest="v1", approved_tools=set())


def test_changed_content_and_new_successful_information_allow_reinspection():
    guard = ToolLoopGuard()
    for number in range(8):
        guard.check_round([pair(value=str(number))], instruction_digest="v1", approved_tools=set())
    for number in range(8):
        guard.check_round([pair(), pair(name="search", query=str(number))], instruction_digest="v1", approved_tools=set())


def test_instruction_change_and_new_approved_operation_reset_read_stagnation():
    guard = ToolLoopGuard()
    for _ in range(2):
        guard.check_round([pair()], instruction_digest="v1", approved_tools=set())
    guard.check_round([pair()], instruction_digest="v2", approved_tools=set())
    guard.check_round([pair(name="write", value="new state")], instruction_digest="v2", approved_tools={"write"})
    guard.check_round([pair()], instruction_digest="v2", approved_tools=set())
    guard.check_round([pair()], instruction_digest="v2", approved_tools=set())


def test_different_failed_arguments_do_not_bypass_failed_round_limit():
    guard = ToolLoopGuard()
    for number in range(2):
        guard.check_round([pair(failed=True, query=str(number))], instruction_digest="v1", approved_tools=set())
    with pytest.raises(AgentLoopDetected, match="失败或被拒绝"):
        guard.check_round([pair(failed=True, query="different again")], instruction_digest="v1", approved_tools=set())


def test_success_interrupts_failed_round_streak_and_new_turn_resets_guard():
    for _ in range(2):
        guard = ToolLoopGuard()
        guard.check_round([pair(failed=True)], instruction_digest="v1", approved_tools=set())
        guard.check_round([pair()], instruction_digest="v1", approved_tools=set())
        guard.check_round([pair(failed=True)], instruction_digest="v1", approved_tools=set())
        guard.check_round([pair(failed=True)], instruction_digest="v1", approved_tools=set())
