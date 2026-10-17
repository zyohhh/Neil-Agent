"""Turn cancellation reaches retry backoff, nested budgets, and blocked SDK streams."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from unittest.mock import MagicMock

import pytest

from neil_agent.config import Settings
from neil_agent.errors import ToolError
from neil_agent.execution_budget import (
    budgeted_sleep,
    check_execution_budget,
    execution_budget_scope,
)
from neil_agent.providers.base import ProviderId
from neil_agent.providers.deepseek import DeepSeekProvider
from neil_agent.providers.openai import OpenAIProvider
from neil_agent.schemas import Message


def test_nested_budget_cannot_discard_parent_cancellation() -> None:
    parent_cancel = Event()
    with execution_budget_scope(cancel=parent_cancel):
        with execution_budget_scope(deadline=monotonic() + 60, cancel=Event()):
            parent_cancel.set()
            with pytest.raises(ToolError, match="已取消"):
                check_execution_budget()


def test_nested_budget_cannot_extend_parent_deadline() -> None:
    with execution_budget_scope(deadline=monotonic() - 1):
        with execution_budget_scope(deadline=monotonic() + 60):
            with pytest.raises(ToolError, match="超时"):
                check_execution_budget()


def test_retry_backoff_observes_cancel_before_waiting_for_full_delay() -> None:
    cancel = Event()
    delays: list[float] = []

    def sleep_and_cancel(delay: float) -> None:
        delays.append(delay)
        cancel.set()

    with execution_budget_scope(cancel=cancel):
        with pytest.raises(ToolError, match="已取消"):
            budgeted_sleep(8, sleep_and_cancel)
    assert sum(delays) < 0.1


@pytest.mark.parametrize("provider", [ProviderId.DEEPSEEK, ProviderId.OPENAI])
def test_cancel_closes_blocked_sdk_stream_before_any_visible_text(
    provider: ProviderId,
) -> None:
    reading = Event()
    closed = Event()
    cancel = Event()

    class BlockingStream:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

        def __iter__(self):
            return self

        def __next__(self):
            reading.set()
            if not closed.wait(2):
                raise AssertionError("SDK stream was not interrupted")
            raise StopIteration

        @property
        def text_stream(self):
            return self

        def close(self):
            closed.set()

        def get_final_message(self):
            raise AssertionError("Cancelled streams must not request a final message")

    client = MagicMock()
    settings = Settings(
        _env_file=None,
        llm_provider=provider,
        llm_model="offline-test-model",
        deepseek_api_key="offline-test",
        openai_api_key="offline-test",
    )
    if provider is ProviderId.DEEPSEEK:
        client.messages.stream.return_value = BlockingStream()
        model = DeepSeekProvider(settings, client=client)
    else:
        client.responses.create.return_value = BlockingStream()
        model = OpenAIProvider(settings, client=client)

    def run():
        with execution_budget_scope(cancel=cancel):
            return list(
                model.stream(
                    [Message(role="user", content="work")], system_prompt="test"
                )
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        assert reading.wait(1)
        cancel.set()
        with pytest.raises(ToolError, match="已取消"):
            future.result(timeout=1)
    assert closed.is_set()
    if provider is ProviderId.DEEPSEEK:
        assert client.messages.stream.call_count == 1
    else:
        assert client.responses.create.call_count == 1
