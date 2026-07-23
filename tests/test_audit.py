"""Tests for bounded metadata-only lifecycle audit records."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from neil_agent.audit import JsonlAuditSink
from neil_agent.errors import AuditError
from neil_agent.hooks import HookEvent, LifecycleHooks
from neil_agent.schemas import (
    ModelResponse,
    ThinkingContent,
    TokenUsage,
    ToolCall,
    ToolResult,
)

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)


def test_audit_records_only_bounded_metadata(tmp_path: Path) -> None:
    sink = JsonlAuditSink(tmp_path, clock=lambda: NOW)
    secret = "DO-NOT-LOG-THIS-CONTENT"
    call = ToolCall(
        id="secret-call-id",
        name="read_file",
        arguments={"path": secret},
    )
    sink.record(
        HookEvent(
            stage="after_model",
            model_round=2,
            message_count=4,
            model_response=ModelResponse(
                content=secret,
                thinking=(ThinkingContent(thinking=secret, signature=secret),),
                tool_calls=(call,),
                usage=TokenUsage(input_tokens=12, output_tokens=3),
            ),
        )
    )
    sink.record(
        HookEvent(
            stage="after_tool",
            tool_call=call,
            tool_result=ToolResult(tool_call_id=call.id, content=secret),
        )
    )

    raw = sink.path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    assert secret not in raw
    assert "secret-call-id" not in raw
    assert records[0]["model_response"] == {
        "text_chars": len(secret),
        "thinking_blocks": 1,
        "tool_calls": 1,
        "usage": {
            "input_tokens": 12,
            "output_tokens": 3,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    assert records[1]["tool"] == {"name": "read_file", "argument_count": 1}
    assert records[1]["tool_result"] == {
        "is_error": False,
        "content_chars": len(secret),
    }


def test_audit_rotates_to_one_bounded_backup(tmp_path: Path) -> None:
    sink = JsonlAuditSink(tmp_path, max_bytes=10_000, clock=lambda: NOW)
    event = HookEvent(stage="before_model", model_round=1, message_count=2)

    for _ in range(150):
        sink.record(event)

    backup = sink.path.with_name("events.jsonl.1")
    assert sink.path.is_file()
    assert backup.is_file()
    assert sink.path.stat().st_size <= 10_000
    assert backup.stat().st_size <= 10_000


def test_audit_rejects_symlink_target_when_supported(tmp_path: Path) -> None:
    outside = tmp_path / "outside.log"
    outside.write_text("outside", encoding="utf-8")
    audit_root = tmp_path / ".neil-agent" / "audit"
    audit_root.mkdir(parents=True)
    target = audit_root / "events.jsonl"
    try:
        os.symlink(outside, target)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不允许创建测试符号链接")
    sink = JsonlAuditSink(tmp_path, clock=lambda: NOW)

    with pytest.raises(AuditError, match="普通文件"):
        sink.record(HookEvent(stage="before_model"))

    assert outside.read_text(encoding="utf-8") == "outside"


def test_register_preflights_existing_log_paths(tmp_path: Path) -> None:
    audit_root = tmp_path / ".neil-agent" / "audit"
    audit_root.mkdir(parents=True)
    (audit_root / "events.jsonl").mkdir()
    sink = JsonlAuditSink(tmp_path, clock=lambda: NOW)

    with pytest.raises(AuditError, match="普通文件"):
        sink.register(LifecycleHooks())
