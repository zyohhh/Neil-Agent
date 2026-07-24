"""Tests for bounded metadata-only lifecycle audit records."""

from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest

from neil_agent.audit import (
    AUDIT_LOCK_FILENAME,
    JsonlAuditSink,
    _AuditFileLock,
)
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


def _write_audit_records(
    workspace: str,
    count: int,
    results: Any,
) -> None:
    try:
        sink = JsonlAuditSink(
            workspace,
            max_bytes=10_000,
            clock=lambda: NOW,
        )
        event = HookEvent(stage="before_model", model_round=1, message_count=2)
        for _ in range(count):
            sink.record(event)
    except Exception as error:  # noqa: BLE001 - child reports test failures.
        results.put(f"{type(error).__name__}: {error}")
    else:
        results.put("")


def _crash_while_holding_audit_lock(workspace: str, ready: Any) -> None:
    sink = JsonlAuditSink(workspace, clock=lambda: NOW)
    sink.validate()
    lock_path = Path(workspace) / ".neil-agent" / "audit" / AUDIT_LOCK_FILENAME
    lock = _AuditFileLock(lock_path, timeout=1, create=False)
    if not lock.acquire():
        os._exit(2)
    ready.set()
    os._exit(0)


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
    status = sink.inspect()
    assert status.lock_available is True
    assert status.current_records == 2
    assert status.backup_records == 0
    assert status.invalid_records == 0


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


def test_audit_inspection_bounds_oversized_lines_and_continues(tmp_path: Path) -> None:
    sink = JsonlAuditSink(tmp_path, clock=lambda: NOW)
    sink.validate()
    valid_record = json.dumps(
        {"version": 1, "stage": "before_model"},
        separators=(",", ":"),
    ).encode()
    sink.path.write_bytes(b"x" * 20_000 + b"\n" + valid_record + b"\n")

    status = sink.inspect()

    assert status.current_records == 2
    assert status.invalid_records == 1


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


def test_audit_lock_timeout_fails_closed_and_stale_file_is_reusable(
    tmp_path: Path,
) -> None:
    sink = JsonlAuditSink(tmp_path, clock=lambda: NOW)
    sink.validate()
    lock_path = tmp_path / ".neil-agent" / "audit" / AUDIT_LOCK_FILENAME
    holder = _AuditFileLock(lock_path, timeout=1, create=False)
    assert holder.acquire() is True
    contender = JsonlAuditSink(
        tmp_path,
        clock=lambda: NOW,
        lock_timeout=0.05,
    )
    try:
        with pytest.raises(AuditError, match="锁.*不可用"):
            contender.record(HookEvent(stage="before_model"))
        status = contender.inspect()
        assert status.lock_available is False
        assert status.current_records is None
    finally:
        holder.close()

    contender.record(HookEvent(stage="before_model"))
    assert contender.inspect().current_records == 1


def test_audit_lock_rejects_symlink_when_supported(tmp_path: Path) -> None:
    sink = JsonlAuditSink(tmp_path, clock=lambda: NOW)
    sink.validate()
    audit_root = tmp_path / ".neil-agent" / "audit"
    lock_path = audit_root / AUDIT_LOCK_FILENAME
    lock_path.unlink()
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"\0")
    try:
        os.symlink(outside, lock_path)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不允许创建测试符号链接")

    with pytest.raises(AuditError, match="锁.*普通文件"):
        sink.record(HookEvent(stage="before_model"))


def test_concurrent_processes_rotate_only_valid_jsonl(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(
            target=_write_audit_records,
            args=(str(tmp_path), 80, results),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)

    assert all(process.exitcode == 0 for process in processes)
    assert [results.get(timeout=5) for _ in processes] == ["", ""]
    audit_root = tmp_path / ".neil-agent" / "audit"
    paths = (audit_root / "events.jsonl", audit_root / "events.jsonl.1")
    assert all(path.is_file() for path in paths)
    for path in paths:
        assert path.stat().st_size <= 10_000
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            assert record["version"] == 1
            assert record["stage"] == "before_model"


def test_process_crash_releases_kernel_lock_without_stale_lock_cleanup(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(
        target=_crash_while_holding_audit_lock,
        args=(str(tmp_path), ready),
    )

    process.start()
    assert ready.wait(timeout=10)
    process.join(timeout=10)
    assert process.exitcode == 0

    sink = JsonlAuditSink(tmp_path, clock=lambda: NOW, lock_timeout=0.2)
    sink.record(HookEvent(stage="before_model"))
    assert sink.inspect().current_records == 1
