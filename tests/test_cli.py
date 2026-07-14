"""Tests for the injectable command-line interface."""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from neil_agent import cli
from neil_agent.config import Settings


def test_run_uses_injected_console(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=tmp_path,
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    console = MagicMock(spec=Console)
    console.input.side_effect = ["/help", "/exit"]

    cli.run(cast(Console, console))

    printed_text = "\n".join(
        str(call.args[0]) for call in console.print.call_args_list if call.args
    )
    assert "可用命令" in printed_text
    assert "Neil Agent 已退出" in printed_text
