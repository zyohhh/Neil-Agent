"""Tests for environment-driven application settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from neil_agent.config import Settings
from neil_agent.providers.base import ProviderId


def test_system_prompt_and_thinking_mode_load_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYSTEM_PROMPT", "You are a patient Python tutor.")
    monkeypatch.setenv("THINKING_ENABLED", "true")

    settings = Settings(_env_file=None, deepseek_api_key="test-key")

    assert settings.system_prompt == "You are a patient Python tutor."
    assert settings.thinking_enabled is True


def test_time_machine_event_persistence_is_explicit_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = Settings(_env_file=None, deepseek_api_key="test-key")
    assert defaults.runtime_event_store_enabled is False

    monkeypatch.setenv("RUNTIME_EVENT_STORE_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_EVENT_STORE_MAX_BYTES", "250000")
    enabled = Settings(_env_file=None, deepseek_api_key="test-key")

    assert enabled.runtime_event_store_enabled is True
    assert enabled.runtime_event_store_max_bytes == 250_000

    with pytest.raises(ValidationError, match="runtime_event_store_max_bytes"):
        Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            runtime_event_store_max_bytes=9_999,
        )


def test_web_runtime_model_allowlist_is_explicit_bounded_and_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = Settings(_env_file=None, deepseek_api_key="test-key")
    assert defaults.web_runtime_model_allowlist == ()

    monkeypatch.setenv(
        "WEB_RUNTIME_MODEL_ALLOWLIST",
        '["deepseek-fast", "deepseek-reasoning"]',
    )
    configured = Settings(_env_file=None, deepseek_api_key="test-key")
    assert configured.web_runtime_model_allowlist == (
        "deepseek-fast",
        "deepseek-reasoning",
    )

    with pytest.raises(ValidationError, match="duplicates"):
        Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            web_runtime_model_allowlist=("duplicate", "duplicate"),
        )
    with pytest.raises(ValidationError, match="surrounding whitespace"):
        Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            web_runtime_model_allowlist=(" unsafe",),
        )


def test_legacy_deepseek_configuration_remains_the_default() -> None:
    settings = Settings(_env_file=None, deepseek_api_key="test-key")

    assert settings.llm_provider is ProviderId.DEEPSEEK
    assert settings.selected_model == settings.deepseek_model
    assert settings.selected_base_url == settings.deepseek_base_url
    assert settings.selected_api_key == settings.deepseek_api_key


def test_default_deepseek_provider_still_requires_its_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ValidationError, match="DEEPSEEK_API_KEY is required"):
        Settings(_env_file=None)


def test_local_provider_does_not_require_any_cloud_api_key() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider=ProviderId.OLLAMA,
        llm_model="qwen-local",
    )

    assert settings.deepseek_api_key is None
    assert settings.selected_api_key is None
    assert settings.selected_api_key_required is False
    assert str(settings.selected_base_url) == "http://localhost:11434/v1"


def test_cloud_provider_validates_only_its_selected_key() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY is required"):
        Settings(
            _env_file=None,
            llm_provider=ProviderId.OPENAI,
            llm_model="configured-openai-model",
        )

    settings = Settings(
        _env_file=None,
        llm_provider=ProviderId.OPENAI,
        llm_model="configured-openai-model",
        openai_api_key="openai-secret",
    )

    assert settings.deepseek_api_key is None
    assert settings.selected_model == "configured-openai-model"
    assert settings.selected_api_key == settings.openai_api_key
    assert str(settings.selected_base_url) == "https://api.openai.com/v1"
    assert "openai-secret" not in repr(settings)


def test_claude_uses_the_project_owned_native_endpoint_by_default() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider=ProviderId.CLAUDE,
        llm_model="configured-claude-model",
        anthropic_api_key="claude-secret",
    )

    assert str(settings.selected_base_url) == "https://api.anthropic.com/"


def test_non_deepseek_provider_requires_explicit_model() -> None:
    with pytest.raises(ValidationError, match="LLM_MODEL is required"):
        Settings(
            _env_file=None,
            llm_provider=ProviderId.VLLM,
        )


def test_generic_model_and_endpoint_override_deepseek_compatibility_fields() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        llm_model="override-model",
        llm_base_url="https://gateway.example/v1",
        llm_allow_custom_base_url=True,
    )

    assert settings.selected_model == "override-model"
    assert str(settings.selected_base_url) == "https://gateway.example/v1"


def test_custom_llm_base_url_requires_explicit_opt_in_for_remote_hosts() -> None:
    with pytest.raises(ValidationError, match="LLM_ALLOW_CUSTOM_BASE_URL"):
        Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            llm_base_url="https://gateway.example/v1",
        )

    settings = Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        llm_base_url="http://127.0.0.1:9000/v1",
    )
    assert str(settings.selected_base_url) == "http://127.0.0.1:9000/v1"


def test_system_prompt_rejects_whitespace_only_value() -> None:
    with pytest.raises(ValidationError, match="system prompt must not be blank"):
        Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            system_prompt="   ",
        )


def test_workspace_and_tool_limit_load_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("MAX_TOOL_ROUNDS", "3")
    monkeypatch.setenv("MAX_CONTEXT_CHARS", "64000")
    monkeypatch.setenv("MAX_RETRIES", "4")
    monkeypatch.setenv("RETRY_BASE_DELAY", "0.5")
    monkeypatch.setenv("RETRY_MAX_DELAY", "6")
    monkeypatch.setenv("COMMAND_TIMEOUT", "45")
    monkeypatch.setenv("MAX_COMMAND_OUTPUT_CHARS", "12000")

    settings = Settings(_env_file=None, deepseek_api_key="test-key")

    assert settings.workspace_root == workspace
    assert settings.max_tool_rounds == 3
    assert settings.max_context_chars == 64_000
    assert settings.max_retries == 4
    assert settings.retry_base_delay == 0.5
    assert settings.retry_max_delay == 6
    assert settings.command_timeout == 45
    assert settings.max_command_output_chars == 12_000


def test_retry_base_delay_cannot_exceed_maximum() -> None:
    with pytest.raises(ValidationError, match="retry base delay cannot exceed"):
        Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            retry_base_delay=10,
            retry_max_delay=5,
        )


def test_sandbox_backend_is_disabled_by_default_and_loads_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SANDBOX_BACKEND", raising=False)
    default_settings = Settings(_env_file=None, deepseek_api_key="test-key")

    monkeypatch.setenv("SANDBOX_BACKEND", "windows-sandbox")
    enabled_settings = Settings(_env_file=None, deepseek_api_key="test-key")

    assert default_settings.sandbox_backend == "disabled"
    assert enabled_settings.sandbox_backend == "windows-sandbox"


def test_sandbox_backend_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError, match="sandbox_backend"):
        Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            sandbox_backend="subprocess",  # type: ignore[arg-type]
        )


def test_sandbox_certification_settings_load_as_explicit_pins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SANDBOX_CERTIFICATION_ROOT", str(tmp_path))
    monkeypatch.setenv("SANDBOX_TRUSTED_REVIEWER", "independent-reviewer")
    monkeypatch.setenv("SANDBOX_TRUSTED_REVIEW_SHA256", "a" * 64)

    settings = Settings(_env_file=None, deepseek_api_key="test-key")

    assert settings.sandbox_certification_root == tmp_path
    assert settings.sandbox_trusted_reviewer == "independent-reviewer"
    assert settings.sandbox_trusted_review_sha256 == "a" * 64
