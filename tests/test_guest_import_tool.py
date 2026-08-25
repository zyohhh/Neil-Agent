"""Tests for the import_guest_export tool and approval binding wiring."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from neil_agent.approval import ApprovalStore, NoninteractiveApprovalBroker
from neil_agent.config import Settings
from neil_agent.errors import ToolError
from neil_agent.host_runtime import HostMode, build_host_runtime
from neil_agent.noninteractive import run_noninteractive
from neil_agent.sandbox_export import build_guest_export_manifest
from neil_agent.schemas import ToolCall
from neil_agent.tools.guest_import import GuestExportImportTools
from neil_agent.tools.registry import ToolRegistry


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="test-key",
        workspace_root=root,
        llm_model="deepseek-test-model",
    )


def _manifest_and_contents() -> tuple[object, dict[str, bytes]]:
    manifest = build_guest_export_manifest(
        run_id="run-01",
        request_hash="a" * 64,
        certification_sha256="b" * 64,
        files=(("out/result.txt", b"imported\n"),),
    )
    return manifest, {"out/result.txt": b"imported\n"}


def test_import_guest_export_tool_uses_guest_binding(tmp_path: Path) -> None:
    from neil_agent.tools.filesystem import FileSystemTools

    filesystem = FileSystemTools(tmp_path)
    tools = GuestExportImportTools(filesystem)
    manifest, contents = _manifest_and_contents()
    digest = tools.stage(manifest, contents)
    registry = ToolRegistry()
    tools.register(registry)

    call = ToolCall(
        id="call-import",
        name="import_guest_export",
        arguments={"manifest_sha256": digest},
    )
    preview = registry.preview(call).content
    binding = registry.resolve_approval_binding(call, preview)
    assert binding is not None
    assert binding.kind == "guest-export-import"

    store = ApprovalStore(tmp_path)
    request = store.create(
        call,
        preview,
        prompt="import guest export",
        instructions="root rules",
        binding=binding,
    )
    assert request.binding_kind == "guest-export-import"
    assert store.matches(
        request,
        call,
        preview,
        prompt="import guest export",
        instructions="root rules",
        binding=binding,
    )


def test_read_only_host_runtime_does_not_register_import_tool(tmp_path: Path) -> None:
    runtime = build_host_runtime(
        _settings(tmp_path),
        mode=HostMode.NONINTERACTIVE_READONLY,
    )
    assert "import_guest_export" not in runtime.profile.tool_names
    assert runtime.guest_import is None


def test_write_host_runtime_registers_import_tool(tmp_path: Path) -> None:
    runtime = build_host_runtime(
        _settings(tmp_path),
        mode=HostMode.NONINTERACTIVE_WRITE,
    )
    assert "import_guest_export" in runtime.profile.tool_names
    assert runtime.guest_import is not None


def test_noninteractive_v2_request_approve_imports_guest_export(tmp_path: Path) -> None:
    runtime = build_host_runtime(
        _settings(tmp_path),
        mode=HostMode.NONINTERACTIVE_WRITE,
    )
    assert runtime.guest_import is not None
    manifest, contents = _manifest_and_contents()
    digest = runtime.guest_import.stage(manifest, contents)
    (tmp_path / "out").mkdir()

    class ImportModel:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, messages, *, system_prompt: str) -> str:
            del messages, system_prompt
            return "done"

        def stream(self, messages, *, system_prompt: str, tools=()):
            del messages, system_prompt, tools
            self.calls += 1
            from neil_agent.schemas import ModelResponse, ToolCall

            if self.calls == 1:
                yield ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="import-1",
                            name="import_guest_export",
                            arguments={"manifest_sha256": digest},
                        ),
                    )
                )
                return
            yield "import complete"
            yield ModelResponse(content="import complete")

    model = ImportModel()
    request_stdout = StringIO()
    request_exit = run_noninteractive(
        _settings(tmp_path),
        "import sandbox export",
        output_format="json",
        stdout=request_stdout,
        stderr=StringIO(),
        protocol_version=2,
        permission_mode="request",
        llm=model,
    )
    request = json.loads(request_stdout.getvalue())
    assert request_exit == 3
    assert request["type"] == "approval_required"
    approval = request["approval_requests"][0]
    assert approval["tool_name"] == "import_guest_export"
    assert approval["binding_kind"] == "guest-export-import"
    approval_id = approval["approval_id"]

    approve_stdout = StringIO()
    approve_exit = run_noninteractive(
        _settings(tmp_path),
        "import sandbox export",
        output_format="json",
        stdout=approve_stdout,
        stderr=StringIO(),
        protocol_version=2,
        permission_mode="approve",
        approval_id=approval_id,
        llm=ImportModel(),
    )
    approved = json.loads(approve_stdout.getvalue())
    assert approve_exit == 0
    assert approved["success"] is True
    assert (tmp_path / "out" / "result.txt").read_text(encoding="utf-8") == "imported\n"


def test_import_guest_export_rejects_unstaged_manifest(tmp_path: Path) -> None:
    from neil_agent.tools.filesystem import FileSystemTools

    tools = GuestExportImportTools(FileSystemTools(tmp_path))
    with pytest.raises(ToolError, match="不存在"):
        tools.preview_import_guest_export("c" * 64)
