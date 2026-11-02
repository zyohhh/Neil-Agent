"""Attempts and usage must be shared across SDK retries and child runtimes."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from time import monotonic
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from anthropic import APIConnectionError as AnthropicConnectionError
from openai import APIConnectionError as OpenAIConnectionError

from neil_agent.agent import Agent
from neil_agent.config import Settings
from neil_agent.errors import NeilAgentError
from neil_agent.execution_budget import execution_budget_scope
from neil_agent.host_runtime import HostMode, build_agent, build_host_runtime
from neil_agent.providers.deepseek import DeepSeekProvider
from neil_agent.providers.openai import OpenAIProvider
from neil_agent.schemas import Message, ModelResponse, TokenUsage, ToolCall
from neil_agent.subtask import SubtaskParentState, subtask_parent_scope
from neil_agent.turn_budget import (
    TurnBudgetExceeded, TurnLimits, model_attempt_scope, model_request_scope, turn_budget_scope,
)


def _provider(kind, *, fail_once=False, **options):
    settings = Settings(
        _env_file=None, llm_provider=kind, llm_model="offline-test-model", deepseek_api_key="offline-fixture",
        openai_api_key="offline-fixture", thinking_enabled=False,
        max_retries=2, **options,
    )
    client = MagicMock()
    if kind == "deepseek":
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="done")], stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=5, output_tokens=2),
        )
        method = client.messages.create
        failure = AnthropicConnectionError(request=httpx.Request("POST", "https://example.invalid"))
        model = DeepSeekProvider(settings, client=client, sleeper=lambda _: None)
    else:
        response = {
            "id": "resp_fixture", "status": "completed",
            "output": [{"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "done", "annotations": []}]}],
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }
        method = client.responses.create
        failure = OpenAIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
        model = OpenAIProvider(settings, client=client, sleeper=lambda _: None)
    method.return_value = response
    method.side_effect = [failure, response] if fail_once else [response]
    return model, method


@pytest.mark.parametrize("kind", ["deepseek", "openai"])
@pytest.mark.parametrize("limit", [1, 2])
def test_sdk_retries_charge_the_same_turn_and_do_not_double_count(kind, limit):
    model, method = _provider(kind, fail_once=True)
    agent = Agent(model, turn_limits=TurnLimits(max_model_attempts=limit))
    if limit == 1:
        with pytest.raises(TurnBudgetExceeded):
            agent.chat("request")
        assert method.call_count == 1
    else:
        assert agent.chat("request") == "done"
        assert method.call_count == 2
    snapshot = agent.last_run_budget
    assert snapshot.model_attempts == limit and snapshot.unreported_attempts == 1
    assert not snapshot.usage_complete
    assert snapshot.usage == (TokenUsage(input_tokens=5, output_tokens=2) if limit == 2 else None)


@pytest.mark.parametrize("kind", ["deepseek", "openai"])
def test_sdk_timeout_is_clamped_to_parent_deadline(kind):
    model, method = _provider(kind)
    with execution_budget_scope(deadline=monotonic() + 2):
        assert model.complete([Message(role="user", content="q")], system_prompt="test") == "done"
    assert 0 < method.call_args.kwargs["timeout"] <= 2


@pytest.mark.parametrize("kind", ["deepseek", "openai"])
def test_injected_real_sdk_cannot_retry_underneath_the_ledger(kind):
    from anthropic import Anthropic
    from openai import OpenAI

    calls = []

    def respond(request):
        assert request.url.host == "example.invalid"
        calls.append(request)
        return httpx.Response(429, request=request, json={"error": {"type": "rate_limit_error", "message": "offline fixture"}}, headers={"retry-after": "0"})

    settings = Settings(
        _env_file=None, llm_provider=kind, llm_model="offline-model", thinking_enabled=False,
        deepseek_api_key="offline-fixture", openai_api_key="offline-fixture",
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        constructor = Anthropic if kind == "deepseek" else OpenAI
        client = constructor(api_key="offline-fixture", base_url="https://example.invalid", http_client=transport, max_retries=2)
        provider_type = DeepSeekProvider if kind == "deepseek" else OpenAIProvider
        model = provider_type(settings, client=client, sleeper=lambda _: None)
        assert client.max_retries == 2 and model._client.max_retries == 0
        agent = Agent(model, turn_limits=TurnLimits(max_model_attempts=1))
        try:
            with pytest.raises(TurnBudgetExceeded):
                agent.chat("request")
            assert len(calls) == 1 and agent.last_run_budget.model_attempts == 1
        finally:
            model._client.close()
            client.close()


@pytest.mark.parametrize("kind", ["deepseek", "openai"])
def test_turn_deadline_closes_an_established_stream_without_visible_output(kind):
    model, _ = _provider(kind)
    closed = Event()

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            closed.set()

        def close(self):
            closed.set()

        def __iter__(self):
            return self

        @property
        def text_stream(self):
            return self

        def __next__(self):
            assert closed.wait(1), "Deadline did not interrupt the SDK stream"
            raise StopIteration

        def get_final_message(self):
            pytest.fail("Expired stream cannot report success")

    if kind == "deepseek":
        method = model._client.messages.stream
    else:
        method = model._client.responses.create
    method.side_effect = None
    method.return_value = Stream()
    agent = Agent(model, turn_limits=TurnLimits(timeout_seconds=0.2))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: list(agent.stream_chat("work")))
        with pytest.raises(TurnBudgetExceeded, match="时限"):
            future.result(timeout=2)
    assert closed.is_set() and not agent.messages
    assert agent.last_run_budget.stop_reason == "deadline"
    assert agent.last_run_budget.unreported_attempts == 1


@pytest.mark.parametrize("kind", ["deepseek", "openai"])
def test_reported_usage_is_retained_for_an_invalid_terminal_response(kind):
    model, method = _provider(kind)
    response = method.return_value
    if kind == "deepseek":
        response.stop_reason = "max_tokens"
    else:
        response["status"] = "incomplete"
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    method.side_effect = [response]
    agent = Agent(model)
    with pytest.raises(NeilAgentError):
        agent.chat("request")
    assert method.call_count == 1 and not agent.messages
    assert agent.last_run_budget.usage == TokenUsage(input_tokens=5, output_tokens=2)
    assert agent.last_run_budget.usage_complete


@pytest.mark.parametrize("kind", ["deepseek", "openai"])
def test_missing_sdk_usage_fields_are_not_reported_as_measured_zero(kind):
    model, method = _provider(kind)
    response = method.return_value
    if kind == "deepseek":
        response.usage = SimpleNamespace(output_tokens=2)
    else:
        response["usage"] = {"output_tokens": 2}
    method.side_effect = [response]
    agent = Agent(model)
    assert agent.chat("request") == "done"
    assert agent.last_run_budget.usage is None
    assert agent.last_run_budget.unreported_attempts == 1
    assert not agent.last_run_budget.usage_complete


def test_fallback_and_sdk_attempts_have_one_reservation_and_missing_usage_is_visible():
    with turn_budget_scope(TurnLimits(max_model_attempts=3)) as budget:
        with model_request_scope() as request:
            request.usage = TokenUsage(input_tokens=2)
        with model_request_scope() as request:
            with model_attempt_scope() as attempt:
                attempt.usage = TokenUsage(output_tokens=3)
            request.usage = TokenUsage(input_tokens=999)  # Must not be counted twice.
        with model_request_scope():
            pass
        with pytest.raises(TurnBudgetExceeded, match="请求尝试"):
            with model_request_scope():
                pytest.fail("No fourth attempt")
    snapshot = budget.snapshot()
    assert snapshot.model_attempts == 3
    assert snapshot.usage == TokenUsage(input_tokens=2, output_tokens=3)
    assert snapshot.unreported_attempts == 1 and not snapshot.usage_complete


def test_reported_token_threshold_stops_next_attempt_but_allows_current_result():
    with turn_budget_scope(TurnLimits(max_reported_tokens=10)) as budget:
        with model_request_scope() as request:
            request.usage = TokenUsage(input_tokens=12)
        with pytest.raises(TurnBudgetExceeded, match="token"):
            with model_request_scope():
                pytest.fail("Threshold must stop the next request")
    assert budget.snapshot().usage.total_tokens == 12
    assert budget.snapshot().usage_complete


def test_child_runtime_cannot_reset_parent_attempt_budget(tmp_path):
    settings = Settings.model_construct(workspace_root=tmp_path, turn_max_model_attempts=2)
    runtime = build_host_runtime(settings, mode=HostMode.CLI)

    class Model:
        calls = 0

        def stream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield ModelResponse(tool_calls=(ToolCall(id="child", name="run_readonly_subtask", arguments={"prompt": "inspect"}),), usage=TokenUsage(input_tokens=3))
            else:
                yield ModelResponse(content="child result", usage=TokenUsage(output_tokens=4))

    model = Model()
    try:
        agent = build_agent(settings, runtime, model)
        with subtask_parent_scope(SubtaskParentState(settings=settings, model=model, parent_run_id="run-fixture")):
            with pytest.raises(TurnBudgetExceeded):
                list(agent.stream_chat("parent request"))
        assert model.calls == 2 and not agent.messages
        assert agent.last_run_budget.model_attempts == 2
        assert agent.last_run_budget.usage == TokenUsage(input_tokens=3, output_tokens=4)
        assert agent.last_run_budget.usage_complete
    finally:
        runtime.close()


def test_no_followup_budget_stops_before_approval_and_file_effects(tmp_path):
    from neil_agent.tools.filesystem import FileSystemTools
    from neil_agent.tools.registry import ToolRegistry

    target = tmp_path / "app.py"
    target.write_text("old", encoding="utf-8")
    registry = ToolRegistry()
    FileSystemTools(tmp_path).register(registry)

    class Model:
        def stream(self, *args, **kwargs):
            yield ModelResponse(tool_calls=(ToolCall(id="write", name="replace_text", arguments={"path": "app.py", "old_text": "old", "new_text": "new"}),))

    agent = Agent(
        Model(), registry=registry, turn_limits=TurnLimits(max_model_attempts=1),
        approval_handler=lambda *_: pytest.fail("Exhausted budget must not request approval"),
    )
    with pytest.raises(TurnBudgetExceeded):
        list(agent.stream_chat("request"))
    assert target.read_text() == "old" and not agent.messages


def test_new_turn_gets_a_new_budget_after_a_failure():
    model, method = _provider("deepseek", fail_once=True)
    agent = Agent(model, turn_limits=TurnLimits(max_model_attempts=1))
    with pytest.raises(TurnBudgetExceeded):
        agent.chat("one")
    assert agent.last_run_budget.stop_reason == "model_attempts"
    assert agent.chat("two") == "done"
    assert method.call_count == 2
    assert agent.last_run_budget.model_attempts == 1
    assert agent.last_run_budget.usage_complete and agent.last_run_budget.stop_reason is None
    agent.clear()
    assert agent.last_run_budget is None


def test_terminal_runtime_event_exposes_shared_budget_without_prompt_text():
    from neil_agent.events import EventBus

    class Model:
        def complete(self, *args, **kwargs):
            return "done"

    observed = []
    bus = EventBus()
    subscription = bus.subscribe(observed.append)
    try:
        assert Agent(Model(), event_bus=bus).chat("PRIVATE_INPUT_BODY") == "done"
        assert bus.flush(1)
        event = next(event for event in observed if event.stage == "agent_turn" and event.status == "succeeded")
        metadata = {item.name: item.value for item in event.metadata}
        assert metadata["shared_model_attempts"] == 1
        assert metadata["shared_unreported_attempts"] == 1
        assert metadata["shared_usage_complete"] is False
        assert "shared_reported_tokens" not in metadata
        assert "PRIVATE_INPUT_BODY" not in event.model_dump_json()
    finally:
        subscription.close()
        bus.close()


def test_nested_scope_cannot_widen_budget_and_context_is_restored():
    with turn_budget_scope(TurnLimits(max_model_attempts=1)) as parent:
        with turn_budget_scope(TurnLimits(max_model_attempts=1000)) as child:
            assert child is parent
            with model_request_scope():
                pass
            with pytest.raises(TurnBudgetExceeded):
                with model_request_scope():
                    pass
    with turn_budget_scope(TurnLimits()) as fresh:
        assert fresh is not parent and fresh.snapshot().model_attempts == 0


def test_turn_deadline_is_clamped_to_an_existing_host_deadline():
    host_deadline = monotonic() + 2
    with execution_budget_scope(deadline=host_deadline):
        with turn_budget_scope(TurnLimits(timeout_seconds=600)) as budget:
            assert budget.deadline == host_deadline
    with execution_budget_scope(deadline=monotonic() - 1):
        with turn_budget_scope(TurnLimits()) as expired:
            with pytest.raises(TurnBudgetExceeded, match="时限"):
                with model_request_scope():
                    pytest.fail("Expired host budget cannot start an attempt")
    assert expired.snapshot().model_attempts == 0


@pytest.mark.parametrize("updates", [
    {"turn_max_model_attempts": 0}, {"turn_max_reported_tokens": 0},
    {"turn_timeout_seconds": float("inf")}, {"turn_timeout_seconds": float("nan")},
    {"repeated_tool_result_limit": 1}, {"failed_tool_round_limit": 0},
])
def test_new_settings_reject_invalid_limits(updates):
    with pytest.raises(ValueError):
        Settings(_env_file=None, deepseek_api_key="offline-fixture", **updates)
