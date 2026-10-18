"""Execution exclusion must cover model I/O, side effects and host persistence."""

import multiprocessing
from pathlib import Path
from threading import Thread

import pytest
from pydantic import SecretStr

from neil_agent.config import Settings
from neil_agent.errors import AgentError, SessionError
from neil_agent.execution_lock import WorkspaceExecutionLock
from neil_agent.host_runtime import HostMode, build_agent, build_host_runtime
from neil_agent.schemas import Message, ModelResponse
from neil_agent.session import SessionStore


class _EchoModel:
    calls = 0

    def complete(self, *args, **kwargs):
        self.calls += 1
        return "reply"

    def stream(self, *args, **kwargs):
        self.calls += 1
        yield "reply"
        yield ModelResponse(content="reply")


def _settings(root):
    return Settings.model_construct(workspace_root=root, deepseek_api_key=SecretStr("unused-fixture"))


def _hold_in_process(root, acquired, release):
    with WorkspaceExecutionLock(root).scope():
        acquired.set()
        if not release.wait(15):
            raise RuntimeError("Lock test did not finish")


def test_other_process_is_rejected_before_model_or_journal(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    acquired, release = context.Event(), context.Event()
    process = context.Process(target=_hold_in_process, args=(tmp_path, acquired, release))
    runtime = build_host_runtime(_settings(tmp_path), mode=HostMode.CLI)
    model = _EchoModel()
    try:
        process.start()
        assert acquired.wait(15)
        agent = build_agent(_settings(tmp_path), runtime, model)
        with pytest.raises(AgentError, match="其他进程"):
            list(agent.stream_chat("must not run"))
        assert model.calls == 0 and runtime.execution_journal.records() == ()
        release.set()
        process.join(15)
        assert process.exitcode == 0
        assert list(agent.stream_chat("can run now")) == ["reply"]
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(5)
        runtime.close()


def test_process_death_releases_kernel_ownership(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    acquired, release = context.Event(), context.Event()
    process = context.Process(target=_hold_in_process, args=(tmp_path, acquired, release))
    process.start()
    try:
        assert acquired.wait(15)
        process.terminate()
        process.join(10)
        with WorkspaceExecutionLock(tmp_path).scope():
            pass
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)


def test_persistence_callback_runs_before_releasing_lock(tmp_path: Path) -> None:
    runtime = build_host_runtime(_settings(tmp_path), mode=HostMode.CLI)
    competing = WorkspaceExecutionLock(tmp_path)
    saved = []

    def persist():
        with pytest.raises(AgentError, match="其他进程"):
            with competing.scope():
                pytest.fail("Workspace became available before persistence")
        saved.append(True)

    try:
        agent = build_agent(_settings(tmp_path), runtime, _EchoModel(), completed_turn_handler=persist)
        assert list(agent.stream_chat("request")) == ["reply"]
        assert saved == [True]
        with competing.scope():
            pass
    finally:
        runtime.close()


def test_stale_session_is_rejected_before_model_request(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    messages = (Message(role="user", content="q"), Message(role="assistant", content="a"))
    snapshot = store.save(store.new_session(), messages, (), None)
    handle = store.handle_for(snapshot)
    store.rename(handle.session_id, "new title", expected_digest=handle.base_digest)
    runtime = build_host_runtime(_settings(tmp_path), mode=HostMode.CLI)
    model = _EchoModel()
    try:
        agent = build_agent(
            _settings(tmp_path), runtime, model,
            before_turn_handler=lambda: store.require_current(handle),
        )
        with pytest.raises(SessionError, match="修改"):
            list(agent.stream_chat("stale request"))
        assert model.calls == 0 and runtime.execution_journal.records() == ()
        with WorkspaceExecutionLock(tmp_path).scope():
            pass
    finally:
        runtime.close()


def test_stream_close_and_callback_failure_release_lock(tmp_path: Path) -> None:
    runtime = build_host_runtime(_settings(tmp_path), mode=HostMode.CLI)
    try:
        agent = build_agent(_settings(tmp_path), runtime, _EchoModel())
        stream = agent.stream_chat("request")
        assert next(stream) == "reply"
        stream.close()
        with WorkspaceExecutionLock(tmp_path).scope():
            pass

        def fail_save():
            raise SessionError("save failed")

        agent = build_agent(_settings(tmp_path), runtime, _EchoModel(), completed_turn_handler=fail_save)
        with pytest.raises(SessionError):
            list(agent.stream_chat("request"))
        with WorkspaceExecutionLock(tmp_path).scope():
            pass
    finally:
        runtime.close()


def test_close_never_releases_an_active_owner(tmp_path: Path) -> None:
    lock = WorkspaceExecutionLock(tmp_path)
    with lock.scope():
        with lock.scope():
            pass
        lock.close()
        with pytest.raises(AgentError):
            with WorkspaceExecutionLock(tmp_path).scope():
                pytest.fail("close released an active owner")
    with pytest.raises(AgentError, match="关闭"):
        with lock.scope():
            pass
    with WorkspaceExecutionLock(tmp_path).scope():
        pass


def test_same_runtime_other_thread_is_rejected(tmp_path: Path) -> None:
    lock = WorkspaceExecutionLock(tmp_path)
    outcomes = []

    def compete():
        try:
            with lock.scope():
                outcomes.append("acquired")
        except AgentError:
            outcomes.append("busy")

    with lock.scope():
        thread = Thread(target=compete)
        thread.start()
        thread.join(5)
    assert outcomes == ["busy"]
