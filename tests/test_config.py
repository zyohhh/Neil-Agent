"""Tests for environment-driven application settings."""

import pytest
from pydantic import ValidationError

from neil_agent.config import Settings


def test_system_prompt_and_thinking_mode_load_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYSTEM_PROMPT", "You are a patient Python tutor.")
    monkeypatch.setenv("THINKING_ENABLED", "true")

    settings = Settings(_env_file=None, deepseek_api_key="test-key")

    assert settings.system_prompt == "You are a patient Python tutor."
    assert settings.thinking_enabled is True


def test_system_prompt_rejects_whitespace_only_value() -> None:
    with pytest.raises(ValidationError, match="system prompt must not be blank"):
        Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            system_prompt="   ",
        )
