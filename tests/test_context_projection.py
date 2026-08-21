"""Tests for shared ContextTomography Web projections."""

from __future__ import annotations

from pathlib import Path

from neil_agent.config import Settings
from neil_agent.context_projection import build_host_context_tomography
from neil_agent.host_runtime import HostMode, build_host_runtime
from neil_agent.schemas import Message, TokenUsage
from neil_agent.web.dto import ContextDto


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=root,
        llm_model="deepseek-test-model",
        max_context_tokens=200_000,
    )


def test_build_host_context_tomography_matches_five_layer_order(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = build_host_runtime(settings, mode=HostMode.WEB)
    messages = (
        Message(role="user", content="hello"),
        Message(role="assistant", content="world"),
    )

    tomography = build_host_context_tomography(
        settings,
        runtime,
        messages,
        last_server_usage=TokenUsage(input_tokens=10, output_tokens=5),
    )
    dto = ContextDto.from_tomography(tomography)

    assert dto.source == "local_estimate"
    assert dto.tomography_schema_version == 2
    assert [layer.kind for layer in dto.layers] == [
        "system",
        "tool_schemas",
        "project_instructions",
        "selected_history",
        "current_chain",
    ]
    assert dto.total_tokens == 15
    assert dto.pressure is not None
    assert dto.pressure.level == "safe"


def test_context_dto_from_tomography_retains_no_message_bodies(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = build_host_runtime(settings, mode=HostMode.WEB)
    secret = "SECRET-CONTEXT-BODY"
    tomography = build_host_context_tomography(
        settings,
        runtime,
        (Message(role="user", content=secret), Message(role="assistant", content=secret)),
    )

    payload = ContextDto.from_tomography(tomography).model_dump(mode="json")

    assert secret not in str(payload)
