"""Security and contract tests for the read-only Web Workbench P1."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from neil_agent.config import Settings
from neil_agent.events import RuntimeEventFactory
from neil_agent.schemas import ActivityEvent, Message, TokenUsage
from neil_agent.session import SessionStore
from neil_agent.task import QualityCheckRecord, TaskStep
from neil_agent.web import WorkbenchController, WorkbenchSnapshotService, create_app
from neil_agent.web.controller import (
    ClientCommand,
    ControllerSubscription,
    TurnCancelled,
)
from neil_agent.web.security import BootstrapSessionStore

NOW = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)
BOOTSTRAP = "test-bootstrap-secret-that-is-long-enough"


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="not-exposed",
        workspace_root=root,
        llm_model="deepseek-test-model",
        max_context_tokens=200_000,
    )


class EchoWorker:
    def run(self, prompt, cancel, on_text, on_activity, on_runtime):  # type: ignore[no-untyped-def]
        on_activity(ActivityEvent(status="running", message="Model request started"))
        factory = RuntimeEventFactory()
        correlation = factory.new_correlation_id("agent_turn")
        started = factory.create(
            stage="agent_turn", status="started", correlation_id=correlation
        )
        on_runtime(started)
        on_text(f"Echo: {prompt}")
        on_runtime(
            factory.create(
                stage="agent_turn",
                status="succeeded",
                correlation_id=correlation,
                parent_event_id=started.event_id,
                metadata={"model_requests": 1, "tool_calls": 0},
            )
        )


class BlockingWorker:
    def __init__(self) -> None:
        self.started = Event()
        self.cancelled = Event()

    def run(self, prompt, cancel, on_text, on_activity, on_runtime):  # type: ignore[no-untyped-def]
        self.started.set()
        if not cancel.wait(2):
            raise AssertionError("test worker was not cancelled")
        self.cancelled.set()
        raise TurnCancelled


def _client(
    root: Path,
    *,
    worker: Any | None = None,
    controller_options: dict[str, Any] | None = None,
) -> TestClient:
    settings = _settings(root)
    service = WorkbenchSnapshotService(settings, clock=lambda: NOW)
    controller = WorkbenchController(
        service,
        worker or EchoWorker(),
        clock=lambda: NOW,
        **(controller_options or {}),
    )
    app = create_app(
        settings,
        bootstrap_token=BOOTSTRAP,
        service=service,
        controller=controller,
    )
    return TestClient(app, base_url="http://testserver")


def _authenticate(client: TestClient) -> None:
    response = client.post(
        "/api/v1/bootstrap",
        headers={"X-Neil-Bootstrap": BOOTSTRAP, "Origin": "http://127.0.0.1:5173"},
    )
    assert response.status_code == 204
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]


def test_health_is_generic_and_snapshot_requires_one_time_bootstrap(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)

    health = client.get("/api/v1/health")
    unauthenticated = client.get("/api/v1/snapshot")
    _authenticate(client)
    replay = client.post(
        "/api/v1/bootstrap",
        headers={"X-Neil-Bootstrap": BOOTSTRAP, "Origin": "http://127.0.0.1:5173"},
    )
    snapshot = client.get("/api/v1/snapshot")

    assert health.json() == {
        "status": "ready",
        "service": "neil-agent-web",
        "schema_version": 1,
        "read_only_tools": True,
        "realtime": True,
    }
    assert unauthenticated.status_code == 401
    assert replay.status_code == 401
    assert snapshot.status_code == 200
    assert snapshot.headers["cache-control"] == "no-store"
    assert snapshot.json()["security"]["write_routes"] == 0
    assert snapshot.json()["security"]["agent_connected"] is True
    assert "not-exposed" not in snapshot.text
    assert str(tmp_path.resolve()) not in snapshot.text


def test_rejects_untrusted_host_origin_and_all_write_routes(tmp_path: Path) -> None:
    client = _client(tmp_path)

    bad_host = client.get("/api/v1/health", headers={"Host": "evil.example"})
    bad_origin = client.post(
        "/api/v1/bootstrap",
        headers={"X-Neil-Bootstrap": BOOTSTRAP, "Origin": "https://evil.example"},
    )
    no_route = client.post("/api/v1/snapshot")

    assert bad_host.status_code == 400
    assert bad_origin.status_code == 403
    assert no_route.status_code == 405


def test_snapshot_projects_saved_metadata_without_message_or_quality_bodies(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "agent.py").write_text("SECRET_SOURCE_BODY", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET_ENV_BODY", encoding="utf-8")
    store = SessionStore(tmp_path, clock=lambda: NOW, id_factory=lambda: "deadbeef")
    handle = store.new_session()
    store.save(
        handle,
        (
            Message(role="user", content="Visible bounded preview"),
            Message(role="assistant", content="SECRET_ASSISTANT_BODY"),
        ),
        (TaskStep("Inspect metadata", "completed"),),
        QualityCheckRecord(
            check="pytest",
            status="passed",
            command="python -m pytest -q",
            exit_code=0,
            output="SECRET_QUALITY_OUTPUT",
        ),
        last_usage=TokenUsage(input_tokens=120, output_tokens=30),
    )
    client = _client(tmp_path)
    _authenticate(client)

    snapshot = client.get("/api/v1/snapshot")
    payload = snapshot.json()

    assert snapshot.status_code == 200
    assert payload["source"] == "live"
    assert payload["provider"]["model"] == "deepseek-test-model"
    assert payload["sessions"]["items"][0]["preview"] == "Visible bounded preview"
    assert payload["task"]["steps"] == [
        {"title": "Inspect metadata", "status": "completed"}
    ]
    assert payload["context"]["total_tokens"] == 150
    assert payload["review"]["quality_check"] == {
        "check": "pytest",
        "status": "passed",
        "exit_code": 0,
    }
    assert "SECRET_ASSISTANT_BODY" not in snapshot.text
    assert "SECRET_QUALITY_OUTPUT" not in snapshot.text
    assert "SECRET_SOURCE_BODY" not in snapshot.text
    assert "SECRET_ENV_BODY" not in snapshot.text


def test_file_tree_is_bounded_and_rejects_sensitive_or_escaping_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "agent.py").write_text("pass", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".env.local").write_text("key=value", encoding="utf-8")
    client = _client(tmp_path)
    _authenticate(client)

    tree = client.get("/api/v1/files/tree?depth=2")
    traversal = client.get("/api/v1/files/tree", params={"path": "../"})
    sensitive = client.get("/api/v1/files/tree", params={"path": ".git"})

    assert tree.status_code == 200
    assert [item["name"] for item in tree.json()["items"]] == ["src"]
    assert tree.json()["items"][0]["children"][0]["name"] == "agent.py"
    assert traversal.status_code == 400
    assert sensitive.status_code == 400


def test_bootstrap_and_session_expiry_fail_closed() -> None:
    now = NOW

    def clock() -> datetime:
        return now

    bootstrap_store = BootstrapSessionStore(BOOTSTRAP, clock=clock)

    now += timedelta(minutes=3)

    assert bootstrap_store.exchange(BOOTSTRAP) is None

    now = NOW
    session_store = BootstrapSessionStore(BOOTSTRAP, clock=clock)
    session = session_store.exchange(BOOTSTRAP)
    assert session is not None
    assert session_store.validate(session.token) is True

    now += timedelta(hours=9)

    assert session_store.validate(session.token) is False


def _ticket(client: TestClient) -> str:
    response = client.get("/api/v1/ws-ticket")
    assert response.status_code == 200
    return str(response.json()["ticket"])


def _command(
    command_id: str,
    command: str,
    revision: int,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "message_type": "command",
        "command_id": command_id,
        "expected_revision": revision,
        "command": command,
        "payload": payload or {},
    }


def _receive_result(socket, command_id: str) -> dict[str, object]:  # type: ignore[no-untyped-def]
    for _ in range(12):
        message = socket.receive_json()
        if (
            message.get("message_type") == "command_result"
            and message.get("command_id") == command_id
        ):
            return message
    raise AssertionError(f"no result received for {command_id}")


def test_ws_ticket_is_single_use_and_origin_is_required(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _authenticate(client)
    ticket = _ticket(client)

    with client.websocket_connect(
        f"/api/v1/events?ticket={ticket}&after=0",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as socket:
        connected = socket.receive_json()
        assert connected["message_type"] == "connected"
        assert connected["sequence"] == 0

    try:
        with client.websocket_connect(
            f"/api/v1/events?ticket={ticket}&after=0",
            headers={"Origin": "http://127.0.0.1:5173"},
        ):
            raise AssertionError("replayed ticket unexpectedly connected")
    except WebSocketDisconnect as error:
        assert error.code == 4401

    second = _ticket(client)
    try:
        with client.websocket_connect(f"/api/v1/events?ticket={second}&after=0"):
            raise AssertionError("origin-less websocket unexpectedly connected")
    except WebSocketDisconnect as error:
        assert error.code == 4401


def test_realtime_start_stream_completion_and_idempotency(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _authenticate(client)
    ticket = _ticket(client)

    with client.websocket_connect(
        f"/api/v1/events?ticket={ticket}&after=0",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as socket:
        connected = socket.receive_json()
        socket.send_json(
            _command("control01", "acquire_control", connected["revision"])
        )
        control = _receive_result(socket, "control01")
        assert control["status"] == "accepted"

        start_command = _command(
            "startturn01",
            "start_turn",
            int(control["revision"]),
            {"prompt": "Inspect the project"},
        )
        socket.send_json(start_command)
        started = _receive_result(socket, "startturn01")
        assert started["status"] == "accepted"
        assert str(started["run_id"]).startswith("run-")

        observed: set[str] = set()
        completed_revision = int(started["revision"])
        for _ in range(20):
            event = socket.receive_json()
            if event.get("message_type") != "event":
                continue
            observed.add(str(event["event_type"]))
            completed_revision = int(event["revision"])
            if (
                event["event_type"] == "run_state"
                and event["payload"]["status"] == "completed"
            ):
                break
        assert {
            "assistant_text_delta",
            "activity",
            "runtime_step",
            "run_state",
        } <= observed

        socket.send_json(start_command)
        replayed = _receive_result(socket, "startturn01")
        assert replayed == started

    snapshot = client.get("/api/v1/snapshot").json()
    assert snapshot["run"]["status"] == "completed"
    assert snapshot["last_sequence"] > 0
    assert snapshot["revision"] >= completed_revision
    assert snapshot["output"][-1]["text"] == "Echo: Inspect the project"
    assert snapshot["timeline"][0]["stage"] == "agent_turn"


def test_single_turn_revision_control_and_cancel(tmp_path: Path) -> None:
    worker = BlockingWorker()
    client = _client(tmp_path, worker=worker)
    _authenticate(client)

    with client.websocket_connect(
        f"/api/v1/events?ticket={_ticket(client)}&after=0",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as socket:
        connected = socket.receive_json()
        socket.send_json(
            _command("control02", "acquire_control", connected["revision"])
        )
        control = _receive_result(socket, "control02")
        socket.send_json(
            _command(
                "startturn02",
                "start_turn",
                int(control["revision"]),
                {"prompt": "Wait"},
            )
        )
        started = _receive_result(socket, "startturn02")
        assert worker.started.wait(1)

        socket.send_json(_command("stale0001", "cancel_turn", int(control["revision"])))
        stale = _receive_result(socket, "stale0001")
        assert stale["code"] == "revision_conflict"

        socket.send_json(_command("cancel001", "cancel_turn", int(started["revision"])))
        cancelled = _receive_result(socket, "cancel001")
        assert cancelled["status"] == "accepted"
        assert worker.cancelled.wait(1)


def test_sequence_gap_and_slow_consumer_invalidate_snapshot(tmp_path: Path) -> None:
    service = WorkbenchSnapshotService(_settings(tmp_path), clock=lambda: NOW)
    controller = WorkbenchController(
        service,
        EchoWorker(),
        clock=lambda: NOW,
        event_history_size=4,
        subscriber_queue_size=1,
    )
    command = ClientCommand.model_validate(_command("control03", "acquire_control", 0))
    assert controller.handle_command("producer", command)["status"] == "accepted"
    revision = 1
    for index in range(3):
        released = controller.handle_command(
            "producer",
            ClientCommand.model_validate(
                _command(f"release{index:02d}", "release_control", revision)
            ),
        )
        revision = int(released["revision"])
        acquired = controller.handle_command(
            "producer",
            ClientCommand.model_validate(
                _command(f"acquire{index:02d}", "acquire_control", revision)
            ),
        )
        revision = int(acquired["revision"])

    async def assert_invalidations() -> None:
        gap = controller.subscribe("gap", 1)
        assert (await gap.queue.get())["payload"]["reason"] == "sequence_gap"

        snapshot = controller.snapshot()
        slow: ControllerSubscription = controller.subscribe(
            "slow", snapshot.last_sequence
        )
        released = controller.handle_command(
            "producer",
            ClientCommand.model_validate(
                _command("release03", "release_control", snapshot.revision)
            ),
        )
        controller.handle_command(
            "producer",
            ClientCommand.model_validate(
                _command("acquire03", "acquire_control", int(released["revision"]))
            ),
        )
        await asyncio.sleep(0)
        assert (await slow.queue.get())["payload"]["reason"] == "slow_client"

    asyncio.run(assert_invalidations())


def test_process_close_signals_active_worker(tmp_path: Path) -> None:
    worker = BlockingWorker()
    service = WorkbenchSnapshotService(_settings(tmp_path), clock=lambda: NOW)
    controller = WorkbenchController(service, worker, clock=lambda: NOW)
    control = controller.handle_command(
        "owner",
        ClientCommand.model_validate(_command("control04", "acquire_control", 0)),
    )
    controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "startturn04",
                "start_turn",
                int(control["revision"]),
                {"prompt": "Wait"},
            )
        ),
    )
    assert worker.started.wait(1)

    controller.close()

    assert worker.cancelled.wait(1)


def test_reconnect_replays_events_after_snapshot_sequence(tmp_path: Path) -> None:
    service = WorkbenchSnapshotService(_settings(tmp_path), clock=lambda: NOW)
    controller = WorkbenchController(service, EchoWorker(), clock=lambda: NOW)

    async def replay() -> None:
        first = controller.subscribe("first", 0)
        controller.handle_command(
            "first",
            ClientCommand.model_validate(_command("control08", "acquire_control", 0)),
        )
        first_event = await first.queue.get()
        controller.unsubscribe("first")
        controller.handle_command(
            "producer",
            ClientCommand.model_validate(
                _command("control09", "acquire_control", int(first_event["revision"]))
            ),
        )

        reconnected = controller.subscribe("reconnected", int(first_event["sequence"]))
        replayed = await reconnected.queue.get()
        assert replayed["sequence"] == int(first_event["sequence"]) + 1

    asyncio.run(replay())


def test_control_lease_allows_only_one_tab_and_disconnect_releases(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    _authenticate(client)
    headers = {"Origin": "http://127.0.0.1:5173"}
    with client.websocket_connect(
        f"/api/v1/events?ticket={_ticket(client)}&after=0", headers=headers
    ) as first:
        first_connected = first.receive_json()
        first.send_json(
            _command("control05", "acquire_control", first_connected["revision"])
        )
        first_control = _receive_result(first, "control05")
        assert first_control["status"] == "accepted"

        with client.websocket_connect(
            f"/api/v1/events?ticket={_ticket(client)}&after=0", headers=headers
        ) as second:
            second_connected = second.receive_json()
            second.send_json(
                _command("control06", "acquire_control", second_connected["revision"])
            )
            second_control = _receive_result(second, "control06")
            assert second_control["code"] == "control_unavailable"

    with client.websocket_connect(
        f"/api/v1/events?ticket={_ticket(client)}&after=0", headers=headers
    ) as replacement:
        connected = replacement.receive_json()
        replacement.send_json(
            _command("control07", "acquire_control", connected["revision"])
        )
        assert _receive_result(replacement, "control07")["status"] == "accepted"
