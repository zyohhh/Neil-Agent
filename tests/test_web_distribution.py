"""P5 packaging, static-integrity, and launcher lifecycle tests."""

from __future__ import annotations

import hashlib
import json
import socket
import time
import urllib.request
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from neil_agent.config import Settings
from neil_agent.web.app import MAX_WEBSOCKET_MESSAGE_BYTES, create_app
from neil_agent.web.assets import (
    ASSET_MANIFEST,
    StaticBundleError,
    packaged_static_root,
    verify_static_bundle,
)
from neil_agent.web.runtime import (
    WebWorkbenchStartupError,
    ensure_port_available,
    run_workbench,
)

BOOTSTRAP = "distribution-bootstrap-secret-that-is-long-enough"


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="not-exposed",
        workspace_root=root,
        llm_model="deepseek-test-model",
    )


def _write_bundle(root: Path, files: dict[str, bytes]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest_files: dict[str, str] = {}
    for relative_name, content in files.items():
        target = root / relative_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest_files[relative_name] = hashlib.sha256(content).hexdigest()
    (root / ASSET_MANIFEST).write_text(
        json.dumps({"schema_version": 1, "files": manifest_files}),
        encoding="utf-8",
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_packaged_frontend_is_complete_and_verified() -> None:
    bundle = verify_static_bundle(packaged_static_root())
    names = {
        path.relative_to(bundle.root).as_posix()
        for path in bundle.root.rglob("*")
        if path.is_file()
    }

    assert bundle.file_count >= 3
    assert bundle.total_bytes > 100_000
    assert "index.html" in names
    assert ASSET_MANIFEST in names
    assert any(name.startswith("assets/") and name.endswith(".js") for name in names)
    assert any(name.startswith("assets/") and name.endswith(".css") for name in names)


def test_static_bundle_fails_closed_on_tamper_extra_file_and_unsafe_manifest(
    tmp_path: Path,
) -> None:
    bundle_root = tmp_path / "bundle"
    _write_bundle(bundle_root, {"index.html": b"<html></html>", "assets/app.js": b"ok"})
    assert verify_static_bundle(bundle_root).file_count == 2

    (bundle_root / "assets" / "app.js").write_bytes(b"modified")
    with pytest.raises(StaticBundleError, match="integrity"):
        verify_static_bundle(bundle_root)

    _write_bundle(bundle_root, {"index.html": b"<html></html>", "assets/app.js": b"ok"})
    (bundle_root / "unexpected.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(StaticBundleError, match="do not match"):
        verify_static_bundle(bundle_root)

    (bundle_root / "unexpected.txt").unlink()
    (bundle_root / ASSET_MANIFEST).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": {
                    "../escape": "0" * 64,
                    "index.html": hashlib.sha256(b"<html></html>").hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StaticBundleError, match="unsafe"):
        verify_static_bundle(bundle_root)


def test_verified_static_responses_are_csp_hardened_and_upgrade_safe(
    tmp_path: Path,
) -> None:
    app = create_app(
        _settings(tmp_path),
        bootstrap_token=BOOTSTRAP,
        static_root=packaged_static_root(),
    )
    with TestClient(app, base_url="http://testserver") as client:
        index = client.get("/")
        assert index.status_code == 200
        assert index.headers["cache-control"] == "no-store"
        assert "'unsafe-inline'" not in index.headers["content-security-policy"]
        asset_name = next(
            path.relative_to(packaged_static_root()).as_posix()
            for path in packaged_static_root().glob("assets/*.js")
        )
        asset = client.get(f"/{asset_name}")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == ("public, max-age=31536000, immutable")
        assert asset.headers["x-content-type-options"] == "nosniff"


def test_port_conflict_fails_before_server_or_browser_start(tmp_path: Path) -> None:
    browser_calls: list[str] = []
    server_created = False

    def server_factory(_config: Any) -> Any:
        nonlocal server_created
        server_created = True
        raise AssertionError("server must not be constructed for an occupied port")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        with pytest.raises(WebWorkbenchStartupError, match="unavailable"):
            run_workbench(
                _settings(tmp_path),
                port=port,
                static_root=packaged_static_root(),
                browser_open=lambda url, **_kwargs: browser_calls.append(url),
                server_factory=server_factory,
            )

    assert browser_calls == []
    assert server_created is False


def test_launcher_binds_loopback_limits_websockets_and_opens_after_start(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    browser_opened = Event()
    captured_url: list[str] = []
    captured_config: list[Any] = []

    class FakeServer:
        started = False
        should_exit = False

        def __init__(self, config: Any) -> None:
            captured_config.append(config)
            self.config = config

        def run(self) -> None:
            self.started = True
            assert browser_opened.wait(2)
            self.should_exit = True
            self.config.app.state.workbench_controller.close()

    def open_browser(url: str, **_kwargs: Any) -> bool:
        captured_url.append(url)
        browser_opened.set()
        return True

    port = _free_port()
    ensure_port_available(port)
    run_workbench(
        _settings(tmp_path),
        port=port,
        static_root=packaged_static_root(),
        browser_open=open_browser,
        server_factory=FakeServer,
    )

    config = captured_config[0]
    assert config.host == "127.0.0.1"
    assert config.port == port
    assert config.ws_max_size == MAX_WEBSOCKET_MESSAGE_BYTES
    assert config.access_log is False
    assert config.server_header is False
    assert len(captured_url) == 1
    assert captured_url[0].startswith(f"http://127.0.0.1:{port}/#bootstrap=")
    output = capsys.readouterr().out
    assert "verified assets" in output
    assert "#bootstrap=" not in output


def test_real_loopback_server_starts_serves_health_and_shuts_down(
    tmp_path: Path,
) -> None:
    port = _free_port()
    server_ready = Event()
    captured_server: list[uvicorn.Server] = []
    failures: list[BaseException] = []

    def server_factory(config: uvicorn.Config) -> uvicorn.Server:
        server = uvicorn.Server(config)
        captured_server.append(server)
        server_ready.set()
        return server

    def serve() -> None:
        try:
            run_workbench(
                _settings(tmp_path),
                port=port,
                open_browser=False,
                static_root=packaged_static_root(),
                server_factory=server_factory,
            )
        except BaseException as error:  # pragma: no cover - surfaced below
            failures.append(error)

    thread = Thread(target=serve, name="web-runtime-smoke", daemon=True)
    thread.start()
    assert server_ready.wait(2)
    server = captured_server[0]
    deadline = time.monotonic() + 5
    while not server.started and not failures and time.monotonic() < deadline:
        time.sleep(0.02)
    assert failures == []
    assert server.started is True

    with urllib.request.urlopen(  # noqa: S310 - fixed loopback smoke target
        f"http://127.0.0.1:{port}/api/v1/health",
        timeout=2,
    ) as response:
        assert response.status == 200
        assert response.headers["Content-Security-Policy"]

    server.should_exit = True
    thread.join(5)
    assert thread.is_alive() is False
    assert failures == []
    ensure_port_available(port)
