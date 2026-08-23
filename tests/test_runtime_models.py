"""Tests for fail-closed Web runtime model selection."""

from pathlib import Path

import pytest

from neil_agent.config import Settings
from neil_agent.providers.base import ProviderId
from neil_agent.runtime_models import (
    prepare_runtime_model_switch,
    runtime_model_catalog,
)


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=root,
        llm_model="deepseek-primary",
        web_runtime_model_allowlist=("deepseek-fast", "deepseek-reasoning"),
    )


def test_runtime_model_switch_rebuilds_settings_and_preserves_catalog(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    prepared = prepare_runtime_model_switch(settings, "deepseek-fast")

    assert prepared.provider is ProviderId.DEEPSEEK
    assert prepared.previous_model == "deepseek-primary"
    assert prepared.target_model == "deepseek-fast"
    assert prepared.changes_model is True
    assert settings.selected_model == "deepseek-primary"
    assert prepared.settings.selected_model == "deepseek-fast"
    assert runtime_model_catalog(prepared.settings).models == (
        "deepseek-fast",
        "deepseek-primary",
        "deepseek-reasoning",
    )

    restored = prepare_runtime_model_switch(
        prepared.settings,
        "deepseek-primary",
    )
    assert runtime_model_catalog(restored.settings).models == (
        "deepseek-primary",
        "deepseek-fast",
        "deepseek-reasoning",
    )


def test_runtime_model_switch_rejects_unlisted_or_ambiguous_targets(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    with pytest.raises(ValueError, match="not allowlisted"):
        prepare_runtime_model_switch(settings, "deepseek-unknown")
    with pytest.raises(ValueError, match="surrounding whitespace"):
        prepare_runtime_model_switch(settings, " deepseek-fast")
