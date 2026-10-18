"""Internal installed-wheel probes, run by an isolated interpreter."""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import version
from pathlib import Path
from threading import Thread
from typing import Any

from pydantic import SecretStr

from . import __version__
from .config import Settings
from .evaluation.catalog import _load_task_ids
from .evaluation.coding_catalog import load_coding_tasks
from .web.assets import packaged_static_root, verify_static_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Internal installed-wheel probe")
    parser.add_argument("mode", choices=("resources", "serve"))
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--port", type=int)
    arguments = parser.parse_args()
    installed = Path(__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    if not installed or version("neil-agent") != __version__:
        raise SystemExit(1)
    if arguments.mode == "resources":
        bundle = verify_static_bundle(packaged_static_root())
        print(
            json.dumps(
                {
                    "installed": True,
                    "version": __version__,
                    "python": sys.version.split()[0],
                    "offline_tasks": len(_load_task_ids()),
                    "coding_tasks": len(load_coding_tasks()),
                    "static_files": bundle.file_count,
                    "dependencies": {
                        name: version(name)
                        for name in (
                            "anthropic",
                            "openai",
                            "pydantic",
                            "fastapi",
                            "uvicorn",
                            "websockets",
                            "textual",
                        )
                    },
                }
            )
        )
        return
    if arguments.workspace is None or arguments.port is None:
        parser.error("serve requires workspace and port")
    from .web.runtime import run_workbench
    import uvicorn

    def factory(config: Any) -> uvicorn.Server:
        server = uvicorn.Server(config)

        def stop_on_input() -> None:
            sys.stdin.readline()
            server.should_exit = True

        Thread(target=stop_on_input, daemon=True).start()
        return server

    # Fixed local settings bypass user environment and .env discovery entirely.
    settings = Settings.model_construct(
        workspace_root=arguments.workspace.resolve(strict=True),
        deepseek_api_key=SecretStr("offline-install-probe"),
    )
    run_workbench(
        settings, port=arguments.port, open_browser=False, server_factory=factory
    )


if __name__ == "__main__":
    main()
