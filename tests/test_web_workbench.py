"""Security and contract tests for the read-only Web Workbench P1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from neil_agent.config import Settings
from neil_agent.schemas import Message, TokenUsage
from neil_agent.session import SessionStore
from neil_agent.task import QualityCheckRecord, TaskStep
from neil_agent.web import WorkbenchSnapshotService, create_app
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


def _client(root: Path) -> TestClient:
    app = create_app(
        _settings(root),
        bootstrap_token=BOOTSTRAP,
        service=WorkbenchSnapshotService(_settings(root), clock=lambda: NOW),
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
        "read_only": True,
    }
    assert unauthenticated.status_code == 401
    assert replay.status_code == 401
    assert snapshot.status_code == 200
    assert snapshot.headers["cache-control"] == "no-store"
    assert snapshot.json()["security"]["write_routes"] == 0
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
