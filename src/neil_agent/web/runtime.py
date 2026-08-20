"""Loopback-only launcher for the packaged Web Workbench."""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import threading
import time
import webbrowser
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import uvicorn
from pydantic import ValidationError

from ..config import Settings, get_settings
from .app import MAX_WEBSOCKET_MESSAGE_BYTES, create_app, loopback_origins
from .assets import StaticBundleError, packaged_static_root, verify_static_bundle

DEFAULT_WEB_PORT = 8765
MIN_WEB_PORT = 1_024
MAX_WEB_PORT = 65_535
STARTUP_WAIT_SECONDS = 15.0
GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 10
SERVER_LOG_LEVEL = "warning"
WEBSOCKET_IMPLEMENTATION = "websockets-sansio"


class WebWorkbenchStartupError(RuntimeError):
    """The local server cannot start safely."""


def _package_version() -> str:
    try:
        return version("neil-agent")
    except PackageNotFoundError:
        return "0.1.0+source"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Neil Agent Web Workbench")
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="start the local service without opening a browser tab",
    )
    parser.add_argument(
        "--allow-vite-dev-origin",
        action="store_true",
        help="also allow the fixed loopback Vite origin on port 5173",
    )
    parser.add_argument("--version", action="version", version=_package_version())
    return parser


def ensure_port_available(port: int) -> None:
    """Fail before creating or disclosing a launch secret if the port is occupied."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))
    except OSError as error:
        raise WebWorkbenchStartupError(
            f"127.0.0.1:{port} is unavailable; choose another --port"
        ) from error


def ensure_websocket_runtime() -> None:
    """Fail before startup when the wheel cannot serve the advertised realtime API."""

    if find_spec("websockets") is None:
        raise WebWorkbenchStartupError(
            "the WebSocket runtime is not installed; reinstall a complete Neil Agent wheel"
        )


def run_workbench(
    settings: Settings,
    *,
    port: int = DEFAULT_WEB_PORT,
    open_browser: bool = True,
    allow_vite_dev_origin: bool = False,
    static_root: Path | None = None,
    browser_open: Callable[..., Any] = webbrowser.open,
    server_factory: Callable[[uvicorn.Config], Any] | None = None,
) -> None:
    """Verify assets, start one loopback server, and open only after it owns the port."""

    if not MIN_WEB_PORT <= port <= MAX_WEB_PORT:
        raise WebWorkbenchStartupError(
            f"port must be between {MIN_WEB_PORT} and {MAX_WEB_PORT}"
        )
    ensure_websocket_runtime()
    bundle_root = static_root or packaged_static_root()
    try:
        bundle = verify_static_bundle(bundle_root)
    except StaticBundleError as error:
        raise WebWorkbenchStartupError(str(error)) from error
    ensure_port_available(port)

    token = secrets.token_urlsafe(32)
    accepted_origins = loopback_origins(port)
    if allow_vite_dev_origin:
        accepted_origins |= loopback_origins(5173)
    app = create_app(
        settings,
        bootstrap_token=token,
        static_root=bundle.root,
        allowed_origins=accepted_origins,
    )
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        access_log=False,
        # Uvicorn's WebSocket INFO message includes the full request target;
        # keep short-lived authentication tickets out of terminal output.
        log_level=SERVER_LOG_LEVEL,
        server_header=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
        ws=WEBSOCKET_IMPLEMENTATION,
        ws_max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
    )
    server = (server_factory or uvicorn.Server)(config)
    launch_url = f"http://127.0.0.1:{port}/#bootstrap={token}"

    print(
        f"Web Workbench {_package_version()} starting on "
        f"http://127.0.0.1:{port}/ ({bundle.file_count} verified assets)",
        flush=True,
    )
    if open_browser:
        threading.Thread(
            target=_open_browser_after_start,
            args=(server, launch_url, browser_open),
            name="neil-agent-web-browser",
            daemon=True,
        ).start()
    try:
        server.run()
    except KeyboardInterrupt:
        # Uvicorn restores and re-raises the captured SIGINT after completing its
        # graceful ASGI shutdown. Treat that expected operator action as success.
        return


def _open_browser_after_start(
    server: Any,
    launch_url: str,
    browser_open: Callable[..., Any],
) -> None:
    deadline = time.monotonic() + STARTUP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if bool(getattr(server, "started", False)):
            browser_open(launch_url, new=2)
            return
        if bool(getattr(server, "should_exit", False)):
            return
        time.sleep(0.05)


def main() -> None:
    """CLI entry point for the installed, self-contained workbench."""

    parser = build_parser()
    arguments = parser.parse_args()
    if not MIN_WEB_PORT <= arguments.port <= MAX_WEB_PORT:
        parser.error(f"--port must be between {MIN_WEB_PORT} and {MAX_WEB_PORT}")
    try:
        settings = get_settings()
    except ValidationError as error:
        parser.error(
            f"invalid Neil Agent configuration: {error.error_count()} error(s)"
        )
    try:
        run_workbench(
            settings,
            port=arguments.port,
            open_browser=(
                not arguments.no_browser
                and os.environ.get("NEIL_AGENT_WEB_NO_BROWSER") != "1"
            ),
            allow_vite_dev_origin=arguments.allow_vite_dev_origin,
        )
    except WebWorkbenchStartupError as error:
        parser.exit(2, f"Web Workbench could not start: {error}\n")


if __name__ == "__main__":
    main()
