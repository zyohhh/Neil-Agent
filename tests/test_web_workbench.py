"""Security and contract tests for the read-only Web Workbench P1."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Any

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from neil_agent.config import Settings
from neil_agent.host_runtime import HostMode, build_host_runtime, observe_host_security
from neil_agent.events import RuntimeEventFactory
from neil_agent.schemas import ActivityEvent, Message, TokenUsage
from neil_agent.schemas import ModelResponse, ToolCall
from neil_agent.session import SessionStore
from neil_agent.task import QualityCheckRecord, TaskStep
from neil_agent.web import WorkbenchController, WorkbenchSnapshotService, create_app
from neil_agent.web.dto import SecurityDto
from neil_agent.web.controller import (
    ClientCommand,
    ControllerSubscription,
    TurnCancelled,
    TurnResult,
)
from neil_agent.web.security import BootstrapSessionStore
from neil_agent.web.pricing import ModelRate, ProviderRateTable, load_rate_table

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
    def run(  # type: ignore[no-untyped-def]
        self,
        prompt,
        cancel,
        on_text,
        on_activity,
        on_runtime,
        request_approval,
        context=None,
    ):
        history = () if context is None else context.messages
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
        return TurnResult(
            messages=(
                *history,
                Message(role="user", content=prompt),
                Message(role="assistant", content=f"Echo: {prompt}"),
            )
        )


class BlockingWorker:
    def __init__(self) -> None:
        self.started = Event()
        self.cancelled = Event()

    def run(
        self,
        prompt,
        cancel,
        on_text,
        on_activity,
        on_runtime,
        request_approval,
        context=None,
    ):  # type: ignore[no-untyped-def]
        self.started.set()
        if not cancel.wait(2):
            raise AssertionError("test worker was not cancelled")
        self.cancelled.set()
        raise TurnCancelled


class ApprovalWorker:
    def __init__(self) -> None:
        self.requested = Event()
        self.finished = Event()
        self.approved: bool | None = None

    def run(  # type: ignore[no-untyped-def]
        self,
        prompt,
        cancel,
        on_text,
        on_activity,
        on_runtime,
        request_approval,
        context=None,
    ):
        self.requested.set()
        self.approved = request_approval(
            ToolCall(id="tool-1", name="write_file", arguments={"path": "demo.txt"}),
            "Write demo.txt\n--- before\n+++ after\n+approved content",
        )
        on_text("approved" if self.approved else "rejected")
        self.finished.set()


class DoubleApprovalWorker:
    def __init__(self) -> None:
        self.first_requested = Event()
        self.second_requested = Event()
        self.finished = Event()
        self.decisions: list[bool] = []

    def run(  # type: ignore[no-untyped-def]
        self,
        prompt,
        cancel,
        on_text,
        on_activity,
        on_runtime,
        request_approval,
        context=None,
    ):
        self.first_requested.set()
        self.decisions.append(
            request_approval(
                ToolCall(id="tool-1", name="write_file"), "Preview first tool"
            )
        )
        self.second_requested.set()
        self.decisions.append(
            request_approval(
                ToolCall(id="tool-2", name="write_file"), "Preview second tool"
            )
        )
        self.finished.set()


class QualityHistoryWorker:
    def run(
        self,
        prompt,
        cancel,
        on_text,
        on_activity,
        on_runtime,
        request_approval,
        context=None,
    ):  # type: ignore[no-untyped-def]
        factory = RuntimeEventFactory()
        for status in ("succeeded", "failed", "skipped"):
            correlation = factory.new_correlation_id("quality_check")
            on_runtime(
                factory.create(
                    stage="quality_check",
                    status="started",
                    correlation_id=correlation,
                )
            )
            on_runtime(
                factory.create(
                    stage="quality_check",
                    status=status,
                    correlation_id=correlation,
                )
            )


class ApprovalModel:
    def __init__(self) -> None:
        self.round = 0

    def complete(self, messages, *, system_prompt):  # type: ignore[no-untyped-def]
        return ""

    def stream(self, messages, *, system_prompt, tools=()):  # type: ignore[no-untyped-def]
        self.round += 1
        if self.round == 1:
            yield ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="tool-1",
                        name="write_file",
                        arguments={"path": "approved.txt", "content": "approved"},
                    ),
                )
            )
            return
        yield "done"
        yield ModelResponse(content="done")


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
    assert client.cookies.get("neil_workbench_csrf")


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


def _git_repository(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    (root / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    (root / "binary.dat").write_bytes(b"\x00\x01\x02")
    (root / "old-name.txt").write_text("rename\n", encoding="utf-8")
    _git(root, "add", "tracked.txt", "binary.dat", "old-name.txt")
    _git(root, "commit", "-q", "-m", "initial")


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
    assert snapshot.json()["security"]["shield_schema_version"] == 2
    assert snapshot.json()["security"]["boundary_watch"]["changes_stable"] is True
    assert len(snapshot.json()["security"]["boundary_watch"]["signals"]) == 4
    assert "not-exposed" not in snapshot.text
    assert str(tmp_path.resolve()) not in snapshot.text


def test_snapshot_security_matches_observed_host_shield(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = build_host_runtime(settings, mode=HostMode.WEB)
    service = WorkbenchSnapshotService(settings, host_runtime=runtime, clock=lambda: NOW)
    expected = SecurityDto.from_security_shield(
        observe_host_security(settings, runtime.registry),
        sandbox_backend=settings.sandbox_backend,
    ).model_dump(mode="json")

    client = _client(tmp_path)
    _authenticate(client)
    snapshot = client.get("/api/v1/snapshot")

    assert snapshot.status_code == 200
    assert snapshot.json()["security"] == expected


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
    assert no_route.status_code == 403


def test_security_headers_and_state_changes_require_origin_and_csrf(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)

    health = client.get("/api/v1/health")
    missing_origin = client.post(
        "/api/v1/bootstrap", headers={"X-Neil-Bootstrap": BOOTSTRAP}
    )
    _authenticate(client)
    csrf = client.cookies.get("neil_workbench_csrf")
    assert csrf is not None
    missing_ticket_origin = client.post(
        "/api/v1/ws-ticket", headers={"X-Neil-CSRF": csrf}
    )
    missing_csrf = client.post(
        "/api/v1/ws-ticket", headers={"Origin": "http://127.0.0.1:5173"}
    )
    wrong_csrf = client.post(
        "/api/v1/ws-ticket",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "X-Neil-CSRF": "wrong-token-that-cannot-match",
        },
    )
    get_ticket = client.get("/api/v1/ws-ticket")

    assert missing_origin.status_code == 403
    assert missing_ticket_origin.status_code == 403
    assert missing_csrf.status_code == 403
    assert wrong_csrf.status_code == 403
    assert get_ticket.status_code == 405
    assert "'unsafe-inline'" not in health.headers["content-security-policy"]
    assert "default-src 'none'" in health.headers["content-security-policy"]
    assert health.headers["x-frame-options"] == "DENY"
    assert health.headers["cross-origin-opener-policy"] == "same-origin"
    assert health.headers["cross-origin-resource-policy"] == "same-origin"
    assert health.headers["referrer-policy"] == "no-referrer"


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


def test_file_tree_revision_supports_incremental_refresh(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "first.py").write_text("pass", encoding="utf-8")
    client = _client(tmp_path)
    _authenticate(client)

    initial = client.get("/api/v1/files/tree?depth=2").json()
    unchanged = client.get(
        "/api/v1/files/tree",
        params={"depth": 2, "revision": initial["revision"]},
    ).json()
    (tmp_path / "src" / "second.py").write_text("pass", encoding="utf-8")
    changed = client.get(
        "/api/v1/files/tree",
        params={"depth": 2, "revision": initial["revision"]},
    ).json()

    assert unchanged["unchanged"] is True
    assert unchanged["items"] == []
    assert changed["unchanged"] is False
    assert changed["revision"] != initial["revision"]
    assert [node["name"] for node in changed["items"][0]["children"]] == [
        "first.py",
        "second.py",
    ]


def test_review_projects_numstat_rename_binary_and_untracked(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "tracked.txt").write_text("one\nchanged\nthree\n", encoding="utf-8")
    (tmp_path / "binary.dat").write_bytes(b"\x00\x01\x02\x03")
    _git(tmp_path, "mv", "old-name.txt", "renamed.txt")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")
    client = _client(tmp_path)
    _authenticate(client)

    payload = client.get("/api/v1/review").json()
    files = {item["path"]: item for item in payload["git"]["files"]}

    assert payload["git"]["revision"] is not None
    assert files["tracked.txt"]["additions"] == 2
    assert files["tracked.txt"]["deletions"] == 1
    assert files["tracked.txt"]["diff_available"] is True
    assert files["binary.dat"]["diff_reason"] == "binary"
    assert files["new.txt"]["kind"] == "untracked"
    assert files["new.txt"]["diff_reason"] == "untracked"
    assert files["renamed.txt"]["kind"] == "renamed"
    assert files["renamed.txt"]["previous_path"] == "old-name.txt"


def test_diff_is_authenticated_bounded_and_bound_to_current_revision(
    tmp_path: Path,
) -> None:
    _git_repository(tmp_path)
    (tmp_path / "tracked.txt").write_text(
        "one\n" + ("changed-content\n" * 5_000), encoding="utf-8"
    )
    (tmp_path / "new.txt").write_text("untracked body", encoding="utf-8")
    client = _client(tmp_path)

    assert (
        client.get(
            "/api/v1/review/diff",
            params={"path": "tracked.txt", "revision": "0" * 16},
        ).status_code
        == 401
    )
    _authenticate(client)
    review = client.get("/api/v1/review").json()
    revision = review["git"]["revision"]
    diff = client.get(
        "/api/v1/review/diff",
        params={"path": "tracked.txt", "revision": revision},
    )
    untracked = client.get(
        "/api/v1/review/diff",
        params={"path": "new.txt", "revision": revision},
    )
    traversal = client.get(
        "/api/v1/review/diff",
        params={"path": "../outside", "revision": revision},
    )
    (tmp_path / "tracked.txt").write_text("revision changed\n", encoding="utf-8")
    stale = client.get(
        "/api/v1/review/diff",
        params={"path": "tracked.txt", "revision": revision},
    )

    assert diff.status_code == 200
    assert diff.json()["available"] is True
    assert diff.json()["truncated"] is True
    assert len(diff.json()["content"]) == 40_000
    assert untracked.json()["reason"] == "untracked"
    assert "untracked body" not in untracked.text
    assert traversal.status_code == 400
    assert stale.json()["reason"] == "stale"
    assert stale.json()["content"] == ""


def test_cost_requires_exact_versioned_rate_and_all_used_token_categories(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path, clock=lambda: NOW, id_factory=lambda: "feedface")
    handle = store.new_session()
    store.save(
        handle,
        (
            Message(role="user", content="Estimate this"),
            Message(role="assistant", content="Done"),
        ),
        (),
        None,
        last_usage=TokenUsage(
            input_tokens=1_000_000,
            output_tokens=500_000,
            cache_read_input_tokens=250_000,
        ),
    )
    complete = ProviderRateTable(
        version="rates-2026-08",
        effective_date=NOW.date(),
        rates=(
            ModelRate(
                provider="deepseek",
                model="deepseek-test-model",
                input_usd_per_million=Decimal("1"),
                output_usd_per_million=Decimal("2"),
                cache_read_usd_per_million=Decimal("0.25"),
                input_token_accounting="input_includes_cache_tokens",
            ),
        ),
    )
    settings = _settings(tmp_path)

    available = WorkbenchSnapshotService(
        settings, clock=lambda: NOW, rate_table=complete
    ).review()
    missing_cache = WorkbenchSnapshotService(
        settings,
        clock=lambda: NOW,
        rate_table=ProviderRateTable(
            version="rates-incomplete",
            effective_date=NOW.date(),
            rates=(
                ModelRate(
                    provider="deepseek",
                    model="deepseek-test-model",
                    input_usd_per_million=Decimal("1"),
                    output_usd_per_million=Decimal("2"),
                ),
            ),
        ),
    ).review()

    assert available.cost_available is True
    assert available.cost.estimated_usd == "1.812500"
    assert available.cost.rate_table_version == "rates-2026-08"
    assert missing_cache.cost_available is False
    assert missing_cache.cost.reason == "cache_rate_missing"

    future = WorkbenchSnapshotService(
        settings,
        clock=lambda: NOW,
        rate_table=ProviderRateTable(
            version="future-rates",
            effective_date=(NOW + timedelta(days=1)).date(),
            rates=complete.rates,
        ),
    ).review()
    assert future.cost_available is False
    assert future.cost.reason == "rate_not_effective"


def test_rate_table_loader_fails_closed_on_unknown_or_incomplete_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rates.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "test-v1",
                "effective_date": "2026-08-13",
                "currency": "USD",
                "rates": [
                    {
                        "provider": "deepseek",
                        "model": "deepseek-test-model",
                        "input_usd_per_million": "1",
                        "output_usd_per_million": "2",
                        "input_token_accounting": "separate_cache_tokens",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = load_rate_table(path)
    assert loaded is not None
    assert loaded.version == "test-v1"

    path.write_text(
        '{"schema_version":1,"version":"bad","extra":true}', encoding="utf-8"
    )
    assert load_rate_table(path) is None


def test_bootstrap_and_session_expiry_fail_closed() -> None:
    now = NOW

    def clock() -> datetime:
        return now

    bootstrap_store = BootstrapSessionStore(BOOTSTRAP, clock=clock)

    now += timedelta(minutes=2)

    assert bootstrap_store.exchange(BOOTSTRAP) is None

    now = NOW
    session_store = BootstrapSessionStore(BOOTSTRAP, clock=clock)
    session = session_store.exchange(BOOTSTRAP)
    assert session is not None
    assert session_store.validate(session.token) is True

    now += timedelta(hours=8)

    assert session_store.validate(session.token) is False


def test_bootstrap_exchange_is_atomic_and_csrf_and_ticket_expire() -> None:
    now = NOW

    def clock() -> datetime:
        return now

    store = BootstrapSessionStore(BOOTSTRAP, clock=clock)
    with ThreadPoolExecutor(max_workers=8) as executor:
        sessions = list(executor.map(store.exchange, [BOOTSTRAP] * 8))
    accepted = [session for session in sessions if session is not None]
    assert len(accepted) == 1
    session = accepted[0]
    assert store.validate_csrf(session.token, session.csrf_token) is True
    assert store.validate_csrf(session.token, "wrong") is False
    ticket = store.issue_ws_ticket(session.token)
    assert ticket is not None

    now += timedelta(seconds=30)

    assert store.consume_ws_ticket(ticket.token) is False


def _ticket(client: TestClient) -> str:
    csrf = client.cookies.get("neil_workbench_csrf")
    assert csrf is not None
    response = client.post(
        "/api/v1/ws-ticket",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "X-Neil-CSRF": csrf,
        },
    )
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

    third = _ticket(client)
    try:
        with client.websocket_connect(
            f"/api/v1/events?ticket={third}&after=0",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Host": "evil.example",
            },
        ):
            raise AssertionError("untrusted WebSocket Host unexpectedly connected")
    except WebSocketDenialResponse as error:
        assert error.status_code == 400

    second = _ticket(client)
    try:
        with client.websocket_connect(f"/api/v1/events?ticket={second}&after=0"):
            raise AssertionError("origin-less websocket unexpectedly connected")
    except WebSocketDisconnect as error:
        assert error.code == 4401


def test_websocket_rejects_oversized_commands(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _authenticate(client)

    with client.websocket_connect(
        f"/api/v1/events?ticket={_ticket(client)}&after=0",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as socket:
        assert socket.receive_json()["message_type"] == "connected"
        socket.send_text("x" * (64 * 1024 + 1))
        error = socket.receive_json()
        assert error["code"] == "message_too_large"
        try:
            socket.receive_json()
            raise AssertionError("oversized WebSocket command remained connected")
        except WebSocketDisconnect as disconnect:
            assert disconnect.code == 4409


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


def test_current_web_turn_keeps_bounded_quality_check_history(tmp_path: Path) -> None:
    client = _client(tmp_path, worker=QualityHistoryWorker())
    _authenticate(client)

    with client.websocket_connect(
        f"/api/v1/events?ticket={_ticket(client)}&after=0",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as socket:
        connected = socket.receive_json()
        socket.send_json(
            _command("qualityctrl", "acquire_control", connected["revision"])
        )
        control = _receive_result(socket, "qualityctrl")
        socket.send_json(
            _command(
                "qualityrun",
                "start_turn",
                int(control["revision"]),
                {"prompt": "Run checks"},
            )
        )
        _receive_result(socket, "qualityrun")
        for _ in range(30):
            event = socket.receive_json()
            if (
                event.get("event_type") == "run_state"
                and event["payload"]["status"] == "completed"
            ):
                break

    review = client.get("/api/v1/snapshot").json()["review"]
    assert [check["status"] for check in review["quality_checks"]] == [
        "passed",
        "failed",
        "not_run",
    ]
    assert review["quality_check"] == review["quality_checks"][-1]


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


def test_process_close_rejects_pending_approval(tmp_path: Path) -> None:
    worker = ApprovalWorker()
    service = WorkbenchSnapshotService(_settings(tmp_path), clock=lambda: NOW)
    controller = WorkbenchController(service, worker, clock=lambda: NOW)
    control = controller.handle_command(
        "owner",
        ClientCommand.model_validate(_command("control-close", "acquire_control", 0)),
    )
    controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "turn-close",
                "start_turn",
                int(control["revision"]),
                {"prompt": "Create one file"},
            )
        ),
    )
    assert worker.requested.wait(1)

    controller.close()

    assert worker.finished.wait(1)
    assert worker.approved is False
    approval = controller.snapshot().approval
    assert approval is not None
    assert approval.state == "stale"
    assert approval.decision_detail == "Workbench is shutting down"


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


def _approval_snapshot(controller: WorkbenchController) -> dict[str, Any]:
    snapshot = controller.snapshot().model_dump(mode="json")
    approval = snapshot["approval"]
    assert approval is not None
    return approval


def test_single_tool_approval_accepts_exact_current_request(tmp_path: Path) -> None:
    worker = ApprovalWorker()
    service = WorkbenchSnapshotService(_settings(tmp_path), clock=lambda: NOW)
    controller = WorkbenchController(service, worker, clock=lambda: NOW)
    control = controller.handle_command(
        "owner",
        ClientCommand.model_validate(_command("approval01", "acquire_control", 0)),
    )
    controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "approval02",
                "start_turn",
                int(control["revision"]),
                {"prompt": "Create one file"},
            )
        ),
    )
    assert worker.requested.wait(1)
    approval = _approval_snapshot(controller)
    snapshot = controller.snapshot()
    assert approval["tool_name"] == "write_file"
    assert "approved content" in approval["preview"]
    assert snapshot.capabilities.can_approve_tool is True
    assert snapshot.review.state == "approval_required"

    decision = controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "approval03",
                "approve_tool",
                snapshot.revision,
                {"request_id": approval["request_id"]},
            )
        ),
    )

    assert decision["status"] == "accepted"
    assert worker.finished.wait(1)
    assert worker.approved is True
    assert controller.snapshot().approval is not None
    assert controller.snapshot().approval.state == "approved"


def test_approval_rejects_stale_revision_wrong_id_and_duplicate(
    tmp_path: Path,
) -> None:
    worker = ApprovalWorker()
    service = WorkbenchSnapshotService(_settings(tmp_path), clock=lambda: NOW)
    controller = WorkbenchController(service, worker, clock=lambda: NOW)
    control = controller.handle_command(
        "owner",
        ClientCommand.model_validate(_command("approval04", "acquire_control", 0)),
    )
    controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "approval05",
                "start_turn",
                int(control["revision"]),
                {"prompt": "Create one file"},
            )
        ),
    )
    assert worker.requested.wait(1)
    snapshot = controller.snapshot()
    approval = _approval_snapshot(controller)

    stale = controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "approval06",
                "approve_tool",
                snapshot.revision - 1,
                {"request_id": approval["request_id"]},
            )
        ),
    )
    wrong = controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "approval07",
                "approve_tool",
                snapshot.revision,
                {"request_id": "approval-" + "0" * 32},
            )
        ),
    )
    assert stale["code"] == "revision_conflict"
    assert wrong["code"] == "approval_stale"

    rejected = controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "approval08",
                "reject_tool",
                snapshot.revision,
                {"request_id": approval["request_id"]},
            )
        ),
    )
    assert rejected["status"] == "accepted"
    assert worker.finished.wait(1)
    assert worker.approved is False
    duplicate = controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "approval09",
                "approve_tool",
                controller.snapshot().revision,
                {"request_id": approval["request_id"]},
            )
        ),
    )
    assert duplicate["code"] == "approval_resolved"


def test_control_disconnect_rejects_pending_approval(tmp_path: Path) -> None:
    worker = ApprovalWorker()
    service = WorkbenchSnapshotService(_settings(tmp_path), clock=lambda: NOW)
    controller = WorkbenchController(service, worker, clock=lambda: NOW)

    async def disconnect() -> None:
        controller.subscribe("owner", 0)
        control = controller.handle_command(
            "owner",
            ClientCommand.model_validate(_command("approval10", "acquire_control", 0)),
        )
        controller.handle_command(
            "owner",
            ClientCommand.model_validate(
                _command(
                    "approval11",
                    "start_turn",
                    int(control["revision"]),
                    {"prompt": "Create one file"},
                )
            ),
        )
        assert worker.requested.wait(1)
        controller.unsubscribe("owner")

    asyncio.run(disconnect())
    assert worker.finished.wait(1)
    assert worker.approved is False
    approval = controller.snapshot().approval
    assert approval is not None
    assert approval.state == "stale"
    assert approval.decision_detail == "Control client disconnected"


def test_multiple_tools_require_separate_approval_requests(tmp_path: Path) -> None:
    worker = DoubleApprovalWorker()
    service = WorkbenchSnapshotService(_settings(tmp_path), clock=lambda: NOW)
    controller = WorkbenchController(service, worker, clock=lambda: NOW)
    control = controller.handle_command(
        "owner",
        ClientCommand.model_validate(_command("approval12", "acquire_control", 0)),
    )
    controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "approval13",
                "start_turn",
                int(control["revision"]),
                {"prompt": "Create two files"},
            )
        ),
    )
    assert worker.first_requested.wait(1)
    first = controller.snapshot().approval
    assert first is not None
    first_decision = controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "approval14",
                "approve_tool",
                controller.snapshot().revision,
                {"request_id": first.request_id},
            )
        ),
    )
    assert first_decision["status"] == "accepted"
    assert worker.second_requested.wait(1)
    second = controller.snapshot().approval
    assert second is not None
    assert second.request_id != first.request_id
    assert second.preview == "Preview second tool"

    second_decision = controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "approval15",
                "reject_tool",
                controller.snapshot().revision,
                {"request_id": second.request_id},
            )
        ),
    )
    assert second_decision["status"] == "accepted"
    assert worker.finished.wait(1)
    assert worker.decisions == [True, False]


def test_agent_worker_revalidates_preview_before_approved_write(
    tmp_path: Path,
) -> None:
    from neil_agent.web.controller import AgentTurnWorker

    model = ApprovalModel()
    worker = AgentTurnWorker(_settings(tmp_path))
    approved = Event()

    from unittest.mock import patch

    with patch("neil_agent.web.controller.create_provider", return_value=model):
        worker.run(
            "Create approved.txt",
            Event(),
            lambda text: None,
            lambda event: None,
            lambda event: None,
            lambda call, preview: approved.set() or True,
        )

    assert approved.is_set()
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "approved"


def test_agent_worker_fails_closed_when_preview_changes_after_approval(
    tmp_path: Path,
) -> None:
    from neil_agent.web.controller import AgentTurnWorker
    from unittest.mock import patch

    target = tmp_path / "approved.txt"
    target.write_text("before", encoding="utf-8")
    model = ApprovalModel()
    worker = AgentTurnWorker(_settings(tmp_path))

    def change_after_preview(call: ToolCall, preview: str) -> bool:
        assert "before" in preview
        target.write_text("concurrent change", encoding="utf-8")
        return True

    with patch("neil_agent.web.controller.create_provider", return_value=model):
        worker.run(
            "Update approved.txt",
            Event(),
            lambda text: None,
            lambda event: None,
            lambda event: None,
            change_after_preview,
        )

    assert target.read_text(encoding="utf-8") == "concurrent change"


def test_approval_expires_and_unblocks_worker(tmp_path: Path) -> None:
    current = [NOW]
    worker = ApprovalWorker()
    service = WorkbenchSnapshotService(_settings(tmp_path), clock=lambda: current[0])
    controller = WorkbenchController(service, worker, clock=lambda: current[0])
    control = controller.handle_command(
        "owner",
        ClientCommand.model_validate(_command("approval16", "acquire_control", 0)),
    )
    controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "approval17",
                "start_turn",
                int(control["revision"]),
                {"prompt": "Create one file"},
            )
        ),
    )
    assert worker.requested.wait(1)

    current[0] += timedelta(minutes=6)

    assert worker.finished.wait(1)
    assert worker.approved is False
    approval = controller.snapshot().approval
    assert approval is not None
    assert approval.state == "expired"


def test_websocket_approves_one_current_tool_request(tmp_path: Path) -> None:
    worker = ApprovalWorker()
    client = _client(tmp_path, worker=worker)
    _authenticate(client)
    with client.websocket_connect(
        f"/api/v1/events?ticket={_ticket(client)}&after=0",
        headers={"Origin": "http://127.0.0.1:5173"},
    ) as socket:
        connected = socket.receive_json()
        socket.send_json(
            _command("approval18", "acquire_control", connected["revision"])
        )
        control = _receive_result(socket, "approval18")
        socket.send_json(
            _command(
                "approval19",
                "start_turn",
                int(control["revision"]),
                {"prompt": "Create one file"},
            )
        )
        _receive_result(socket, "approval19")
        approval_event = None
        for _ in range(12):
            event = socket.receive_json()
            if event.get("event_type") == "approval_requested":
                approval_event = event
                break
        assert approval_event is not None
        request_id = approval_event["payload"]["approval"]["request_id"]
        socket.send_json(
            _command(
                "approval20",
                "approve_tool",
                int(approval_event["revision"]),
                {"request_id": request_id},
            )
        )
        assert _receive_result(socket, "approval20")["status"] == "accepted"
        assert worker.finished.wait(1)
        assert worker.approved is True


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


def _wait_for_status(controller: WorkbenchController, status: str) -> None:
    for _ in range(100):
        if controller.snapshot().run.status == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for run status {status}")


def test_completed_web_turn_persists_session_and_restores_history(
    tmp_path: Path,
) -> None:
    service = WorkbenchSnapshotService(_settings(tmp_path), clock=lambda: NOW)
    controller = WorkbenchController(service, EchoWorker(), clock=lambda: NOW)
    control = controller.handle_command(
        "owner",
        ClientCommand.model_validate(_command("persist01", "acquire_control", 0)),
    )
    first = controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "persist02",
                "start_turn",
                int(control["revision"]),
                {"prompt": "Remember this"},
            )
        ),
    )
    assert first["status"] == "accepted"
    _wait_for_status(controller, "completed")

    snapshot = controller.snapshot()
    session_id = snapshot.sessions.active_session_id
    assert session_id is not None
    loaded = service.session_store.load(session_id)
    assert [message.content for message in loaded.messages] == [
        "Remember this",
        "Echo: Remember this",
    ]

    second = controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "persist03",
                "start_turn",
                snapshot.revision,
                {"prompt": "Continue"},
            )
        ),
    )
    assert second["status"] == "accepted"
    _wait_for_status(controller, "completed")
    restored = service.session_store.load(session_id)
    assert [message.content for message in restored.messages] == [
        "Remember this",
        "Echo: Remember this",
        "Continue",
        "Echo: Continue",
    ]


def test_select_session_and_new_session_restore_isolated_histories(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path, clock=lambda: NOW, id_factory=lambda: "aaaaaaaa")
    first_handle = store.new_session()
    store.save(
        first_handle,
        (
            Message(role="user", content="First session prompt"),
            Message(role="assistant", content="First session answer"),
        ),
        (),
        None,
    )
    later = NOW + timedelta(seconds=1)
    store = SessionStore(tmp_path, clock=lambda: later, id_factory=lambda: "bbbbbbbb")
    second_handle = store.new_session()
    store.save(
        second_handle,
        (
            Message(role="user", content="Second session prompt"),
            Message(role="assistant", content="Second session answer"),
        ),
        (TaskStep("Later work", "in_progress"),),
        None,
    )

    service = WorkbenchSnapshotService(_settings(tmp_path), clock=lambda: NOW)
    controller = WorkbenchController(service, EchoWorker(), clock=lambda: NOW)
    snapshot = controller.snapshot()
    assert snapshot.sessions.active_session_id == second_handle.session_id
    assert snapshot.task.steps[0].title == "Later work"

    control = controller.handle_command(
        "owner",
        ClientCommand.model_validate(_command("select01", "acquire_control", 0)),
    )
    selected = controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "select02",
                "select_session",
                int(control["revision"]),
                {"session_id": first_handle.session_id},
            )
        ),
    )
    assert selected["status"] == "accepted"
    snapshot = controller.snapshot()
    assert snapshot.sessions.active_session_id == first_handle.session_id
    assert snapshot.task.steps == ()

    created = controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command("select03", "new_session", snapshot.revision)
        ),
    )
    assert created["status"] == "accepted"
    snapshot = controller.snapshot()
    assert snapshot.sessions.active_session_id != first_handle.session_id
    assert snapshot.sessions.active_session_id != second_handle.session_id
    assert snapshot.task.source == "unavailable"

    started = controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "select04",
                "start_turn",
                snapshot.revision,
                {"prompt": "Fresh"},
            )
        ),
    )
    assert started["status"] == "accepted"
    _wait_for_status(controller, "completed")
    active_id = controller.snapshot().sessions.active_session_id
    assert active_id is not None
    loaded = service.session_store.load(active_id)
    assert [message.content for message in loaded.messages] == [
        "Fresh",
        "Echo: Fresh",
    ]


def test_select_session_rejected_during_active_turn(tmp_path: Path) -> None:
    worker = BlockingWorker()
    service = WorkbenchSnapshotService(_settings(tmp_path), clock=lambda: NOW)
    controller = WorkbenchController(service, worker, clock=lambda: NOW)
    control = controller.handle_command(
        "owner",
        ClientCommand.model_validate(_command("lockctrl", "acquire_control", 0)),
    )
    controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "lockturn1",
                "start_turn",
                int(control["revision"]),
                {"prompt": "Hold"},
            )
        ),
    )
    assert worker.started.wait(1)
    rejected = controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command(
                "locksess1",
                "new_session",
                controller.snapshot().revision,
            )
        ),
    )
    assert rejected["status"] == "rejected"
    assert rejected["code"] == "run_conflict"
    controller.handle_command(
        "owner",
        ClientCommand.model_validate(
            _command("lockcanc1", "cancel_turn", controller.snapshot().revision)
        ),
    )
    assert worker.cancelled.wait(1)
    _wait_for_status(controller, "cancelled")
