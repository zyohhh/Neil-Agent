"""Model preparation failures must preserve a usable current runtime."""

from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

from neil_agent.config import Settings
from neil_agent.host_runtime import HostMode, build_host_runtime
from neil_agent.schemas import Message
from neil_agent.web import service as service_module
from neil_agent.web.controller import (
    ClientCommand,
    CompletedTurnState,
    WorkbenchController,
)
from neil_agent.web.service import WorkbenchSnapshotService


class Worker:
    def run(self, *args, **kwargs):
        raise AssertionError("Model switching does not execute a turn")


def _command(command: str, revision: int, *, model: str | None = None) -> ClientCommand:
    return ClientCommand.model_validate(
        {
            "protocol_version": 1,
            "message_type": "command",
            "command_id": f"test-{command}-{revision}",
            "expected_revision": revision,
            "command": command,
            "payload": {} if model is None else {"model": model},
        }
    )


def _controller(
    tmp_path: Path,
) -> tuple[WorkbenchSnapshotService, WorkbenchController, int]:
    settings = Settings(
        _env_file=None,
        workspace_root=tmp_path,
        deepseek_api_key="test",
        llm_model="current-model",
        web_runtime_model_allowlist=("next-model",),
    )
    service = WorkbenchSnapshotService(settings)
    controller = WorkbenchController(
        service, Worker(), worker_factory=lambda _: Worker()
    )
    acquired = controller.handle_command("owner", _command("acquire_control", 0))
    return service, controller, int(acquired["revision"])


def _switch(controller: WorkbenchController, revision: int):
    return controller.handle_command(
        "owner", _command("switch_model", revision, model="next-model")
    )


def test_runtime_rebuild_failure_preserves_settings_worker_and_open_tools(
    tmp_path: Path, monkeypatch
) -> None:
    service, controller, revision = _controller(tmp_path)
    old_runtime, old_settings, old_worker = (
        service.host_runtime,
        service.settings,
        controller._worker,
    )
    monkeypatch.setattr(
        service_module, "build_host_runtime", Mock(side_effect=OSError("cannot build"))
    )
    result = _switch(controller, revision)
    assert result["code"] == "model_switch_failed"
    assert service.host_runtime is old_runtime and not old_runtime.closed
    assert (
        service.settings is old_settings
        and controller._runtime_settings is old_settings
    )
    assert controller._worker is old_worker and not controller._runtime_has_switched
    assert service.registry.definitions
    assert controller.snapshot().provider.model == "current-model"
    controller.close()


@pytest.mark.parametrize(
    "failure_stage", ["factory", "projection", "event_projection", "revision"]
)
def test_failed_or_conflicting_preparation_disposes_candidate(
    tmp_path: Path, monkeypatch, failure_stage: str
) -> None:
    service, controller, revision = _controller(tmp_path)
    old_runtime = service.host_runtime
    candidate = build_host_runtime(service.settings, mode=HostMode.WEB)
    monkeypatch.setattr(service, "prepare_host_runtime", lambda _: candidate)
    if failure_stage == "factory":
        controller._worker_factory = Mock(
            side_effect=RuntimeError("cannot prepare worker")
        )
    elif failure_stage == "projection":
        monkeypatch.setattr(
            service, "snapshot", Mock(side_effect=OSError("cannot project"))
        )
    elif failure_stage == "event_projection":
        monkeypatch.setattr(
            controller,
            "_model_changed_payload_locked",
            Mock(side_effect=ValueError("cannot project event")),
        )
    else:
        # Force a session change after candidate preparation but before commit.
        def change_session(_):
            with controller._lock:
                controller._active_handle = controller._session_store.new_session()
            return Worker()

        controller._worker_factory = change_session
    result = _switch(controller, revision)
    assert result["status"] == "rejected"
    assert candidate.closed and candidate.registry.definitions == ()
    assert service.host_runtime is old_runtime and not old_runtime.closed
    assert controller._runtime_settings.selected_model == "current-model"
    controller.close()


def test_successful_switch_commits_before_disposing_previous_runtime(
    tmp_path: Path,
) -> None:
    service, controller, revision = _controller(tmp_path)
    old_runtime = service.host_runtime
    observations: list[str] = []
    old_runtime._disposers.append(
        lambda: observations.append(service.settings.selected_model)
    )
    result = _switch(controller, revision)
    assert result["status"] == "accepted"
    assert old_runtime.closed and old_runtime.registry.definitions == ()
    assert observations == ["next-model"]
    assert service.host_runtime is not old_runtime and not service.host_runtime.closed
    assert service.settings is controller._runtime_settings
    assert controller.snapshot().provider.model == "next-model"
    controller.close()


def test_cleanup_failure_does_not_rollback_committed_model(tmp_path: Path) -> None:
    service, controller, revision = _controller(tmp_path)
    old_runtime = service.host_runtime

    def fail_close():
        raise OSError("cleanup failure")

    old_runtime._disposers.append(fail_close)
    result = _switch(controller, revision)
    assert result["status"] == "accepted"
    assert old_runtime.closed and old_runtime.registry.definitions == ()
    assert service.host_runtime is not old_runtime and service.registry.definitions
    assert service.settings.selected_model == "next-model"
    assert controller.snapshot().provider.model == "next-model"
    controller.close()


def test_model_event_failure_keeps_committed_state_and_cleans_previous(
    tmp_path: Path, monkeypatch
) -> None:
    service, controller, revision = _controller(tmp_path)
    old_runtime = service.host_runtime
    monkeypatch.setattr(
        controller, "_publish", Mock(side_effect=RuntimeError("subscriber loop closed"))
    )
    result = _switch(controller, revision)
    assert result["status"] == "accepted" and result["revision"] > revision
    assert service.settings.selected_model == "next-model"
    assert service.settings is controller._runtime_settings
    assert old_runtime.closed and old_runtime.registry.definitions == ()
    assert not service.host_runtime.closed and service.registry.definitions
    monkeypatch.undo()
    controller.close()


def test_service_replace_also_preserves_runtime_on_preparation_failure(
    tmp_path: Path, monkeypatch
) -> None:
    service, controller, _ = _controller(tmp_path)
    old_runtime, old_settings = service.host_runtime, service.settings
    monkeypatch.setattr(
        service, "prepare_host_runtime", Mock(side_effect=ValueError("cannot build"))
    )
    with pytest.raises(ValueError, match="cannot build"):
        service.replace_host_runtime(old_settings)
    assert service.host_runtime is old_runtime and not old_runtime.closed
    assert service.settings is old_settings
    controller.close()


def test_candidate_cleanup_failure_preserves_original_rejection(
    tmp_path: Path, monkeypatch
) -> None:
    service, controller, revision = _controller(tmp_path)
    old_runtime = service.host_runtime
    candidate = build_host_runtime(service.settings, mode=HostMode.WEB)
    candidate._disposers.append(Mock(side_effect=OSError("candidate cleanup failure")))
    monkeypatch.setattr(service, "prepare_host_runtime", lambda _: candidate)
    controller._worker_factory = Mock(side_effect=ValueError("worker failure"))
    result = _switch(controller, revision)
    assert result["code"] == "model_switch_failed"
    assert service.host_runtime is old_runtime and not old_runtime.closed
    assert candidate.closed and candidate.registry.definitions == ()
    controller.close()


@pytest.mark.parametrize("conflict", [False, True])
def test_web_save_settles_journal_and_preserves_concurrent_history(
    tmp_path: Path, conflict: bool
) -> None:
    service, controller, _ = _controller(tmp_path)
    store = service.session_store
    handle = controller._active_handle
    if conflict:
        initial = store.save(
            handle,
            (
                Message(role="user", content="old"),
                Message(role="assistant", content="old"),
            ),
            (),
            None,
        )
        handle = store.handle_for(initial)
        controller._active_handle = handle
        controller._active_snapshot = initial
        concurrent = store.rename(handle.session_id, "concurrent change")
    journal = service.host_runtime.execution_journal
    assert journal is not None
    execution_id = journal.begin(handle.session_id)
    index = journal.request(execution_id, "write_file", requires_approval=True)
    journal.operation(execution_id, index, "started")
    journal.operation(execution_id, index, "succeeded")
    journal.finish(execution_id, "completed")
    completed = CompletedTurnState(
        messages=(
            Message(role="user", content="new"),
            Message(role="assistant", content="new"),
        ),
        steps=(),
        latest_quality_check=None,
        last_usage=None,
        execution_id=execution_id,
    )
    controller._run = controller._run.model_copy(
        update={"run_id": "test-run", "status": "running"}
    )
    controller._cancel = Event()
    success = controller._save_completed_turn(
        "test-run", handle, controller._active_snapshot, completed, service.settings
    )
    assert success is not conflict
    record = journal.load(execution_id)
    assert record.session_id == handle.session_id
    assert record.status == ("save_failed" if conflict else "committed")
    if conflict:
        assert store.load(handle.session_id) == concurrent
    else:
        assert controller._active_handle.base_digest is not None
    controller.close()
