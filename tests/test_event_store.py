"""Tests for optional, bounded JSONL runtime event persistence."""

from __future__ import annotations

import multiprocessing
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from neil_agent.errors import EventStoreError
from neil_agent.event_store import (
    EVENT_STORE_LOCK_FILENAME,
    JsonlEventStore,
    _EventStoreFileLock,
)
from neil_agent.events import (
    EventBus,
    RuntimeEvent,
    RuntimeEventEmitter,
    RuntimeStage,
    RuntimeStatus,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _event(
    number: int,
    *,
    stage: RuntimeStage = "agent_turn",
    status: RuntimeStatus = "started",
) -> RuntimeEvent:
    prefix = {
        "agent_turn": "turn",
        "model_request": "model",
        "tool_call": "tool",
        "approval": "approval",
        "quality_check": "check",
    }[stage]
    return RuntimeEvent(
        event_id=f"evt-{number:032x}",
        correlation_id=f"{prefix}-{number:032x}",
        timestamp=NOW + timedelta(milliseconds=number),
        stage=stage,
        status=status,
    )


def _write_event_records(
    workspace: str,
    start: int,
    count: int,
    results: Any,
) -> None:
    try:
        store = JsonlEventStore(workspace, max_bytes=10_000)
        for number in range(start, start + count):
            store.record(_event(number))
    except Exception as error:  # noqa: BLE001 - child reports test failures.
        results.put(f"{type(error).__name__}: {error}")
    else:
        results.put("")


def test_event_store_has_no_side_effect_until_explicitly_enabled(
    tmp_path: Path,
) -> None:
    store = JsonlEventStore(tmp_path)

    assert not store.path.parent.exists()

    bus = EventBus()
    subscription = store.register(bus)
    emitter = RuntimeEventEmitter(bus)
    span = emitter.start("agent_turn", metadata={"input_chars": 5})
    emitter.finish(
        span,
        "succeeded",
        metadata={
            "model_requests": 1,
            "tool_calls": 0,
            "response_chars": 4,
            "elapsed_ms": 3,
        },
    )

    assert bus.flush(2)
    subscription.close()
    retained = store.load()
    assert [event.status for event in retained] == ["started", "succeeded"]
    assert retained[0].correlation_id == retained[1].correlation_id
    assert bus.close()


def test_event_store_round_trips_strict_versioned_records(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path)
    events = (
        _event(1),
        _event(2, stage="model_request"),
    )

    for event in events:
        store.record(event)

    assert store.load() == events
    raw = store.path.read_text(encoding="utf-8")
    assert raw.count('"version":1') == 2
    assert raw.endswith("\n")


def test_event_store_validates_all_records_but_retains_only_requested_tail(
    tmp_path: Path,
) -> None:
    store = JsonlEventStore(tmp_path)
    events = tuple(_event(number) for number in range(1, 7))
    for event in events:
        store.record(event)

    assert store.load(max_records=2) == events[-2:]
    with pytest.raises(EventStoreError, match="至少为 1"):
        store.load(max_records=0)
    original = store.path.read_bytes()
    store.path.write_bytes(b'{"version":2}\n' + original)
    with pytest.raises(EventStoreError, match="格式无效"):
        store.load(max_records=1)


def test_event_store_rotates_to_one_bounded_backup(tmp_path: Path) -> None:
    max_bytes = 10_000
    store = JsonlEventStore(tmp_path, max_bytes=max_bytes)
    events = tuple(_event(number) for number in range(1, 121))

    for event in events:
        store.record(event)

    retained = store.load()
    assert store.path.stat().st_size <= max_bytes
    assert store.backup_path.stat().st_size <= max_bytes
    assert 1 < len(retained) < len(events)
    assert retained[-1] == events[-1]
    assert store.load(max_records=5) == retained[-5:]
    assert [event.timestamp for event in retained] == sorted(
        event.timestamp for event in retained
    )
    assert len(list(store.path.parent.glob("events.jsonl.*"))) == 1


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b'{"version":2}\n', "格式无效"),
        (b'{"version":1}', "不完整记录"),
        (b"x" * 8_193, "超过大小上限"),
    ],
)
def test_event_store_rejects_invalid_or_partial_records(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    store = JsonlEventStore(tmp_path)
    store.validate()
    store.path.write_bytes(payload)

    with pytest.raises(EventStoreError, match=message):
        store.load()


def test_event_store_rejects_symlink_replacement(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path)
    store.record(_event(1))
    external = tmp_path / "external.jsonl"
    external.write_text("", encoding="utf-8")
    store.path.unlink()
    try:
        os.symlink(external, store.path)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    with pytest.raises(EventStoreError, match="真实普通文件"):
        store.record(_event(2))


def test_event_store_lock_timeout_fails_closed_and_recovers(
    tmp_path: Path,
) -> None:
    store = JsonlEventStore(tmp_path)
    store.validate()
    lock_path = store.path.parent / EVENT_STORE_LOCK_FILENAME
    holder = _EventStoreFileLock(lock_path, timeout=1, create=False)
    assert holder.acquire() is True
    contender = JsonlEventStore(tmp_path, lock_timeout=0.05)
    try:
        with pytest.raises(EventStoreError, match="锁.*不可用"):
            contender.record(_event(10))
    finally:
        holder.close()

    contender.record(_event(11))
    assert contender.load() == (_event(11),)


def test_concurrent_processes_rotate_only_valid_runtime_events(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(
            target=_write_event_records,
            args=(str(tmp_path), start, 80, results),
        )
        for start in (1, 1_001)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)

    assert all(process.exitcode == 0 for process in processes)
    assert [results.get(timeout=5) for _ in processes] == ["", ""]
    store = JsonlEventStore(tmp_path, max_bytes=10_000)
    assert 0 < len(store.load()) < 160
    assert store.path.stat().st_size <= 10_000
    assert store.backup_path.stat().st_size <= 10_000
