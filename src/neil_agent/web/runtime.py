"""Loopback-only launcher for the local Web Workbench."""

from __future__ import annotations

import argparse
import os
import secrets
import threading
import webbrowser
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from ..config import get_settings
from .app import create_app

DEFAULT_WEB_PORT = 8765


def main() -> None:
    """Serve the built workbench on loopback with an ephemeral bootstrap token."""

    parser = argparse.ArgumentParser(description="Run the Neil Agent Web Workbench")
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT)
    arguments = parser.parse_args()
    if not 1_024 <= arguments.port <= 65_535:
        parser.error("--port must be between 1024 and 65535")
    try:
        settings = get_settings()
    except ValidationError as error:
        parser.error(
            f"invalid Neil Agent configuration: {error.error_count()} error(s)"
        )
    token = secrets.token_urlsafe(32)
    static_root = Path.cwd() / "web" / "dist"
    app = create_app(settings, bootstrap_token=token, static_root=static_root)
    launch_url = f"http://127.0.0.1:{arguments.port}/#bootstrap={token}"
    print(
        f"Web Workbench starting on http://127.0.0.1:{arguments.port}/",
        flush=True,
    )
    if os.environ.get("NEIL_AGENT_WEB_NO_BROWSER") != "1":
        threading.Timer(
            0.5, webbrowser.open, args=(launch_url,), kwargs={"new": 2}
        ).start()
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=arguments.port,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
