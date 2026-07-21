"""Tests for versioned, atomic local session snapshots."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from neil_agent import session as session_module
from neil_agent.errors import SessionError
from neil_agent.schemas import Message
from neil_agent.session import MAX_SESSION_TITLE_CHARS, SessionStore
from neil_agent.task import QualityCheckRecord, TaskStep

NOW = datetime(2026, 7, 20, 8, 30, tzinfo=timezone.utc)


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(
        tmp_path,
        clock=lambda: NOW,
        id_factory=lambda: "deadbeef",
    )


def _messages(reply: str = "done") -> tuple[Message, ...]:
    return (
        Message(role="user", content="inspect the project"),
        Message(role="assistant", content=reply),
    )


def test_session_round_trip_is_versioned_and_excludes_environment_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-be-persisted")
    store = _store(tmp_path)
    handle = store.new_session()
    steps = (
        TaskStep("Inspect", "completed"),
        TaskStep("Verify", "in_progress"),
    )
    quality = QualityCheckRecord(
        check="pytest",
        status="passed",
        command="python -m pytest -q",
        exit_code=0,
        output="68 passed",
    )

    saved = store.save(handle, _messages(), steps, quality)
    loaded = store.load(handle.session_id)
    index = store.list_sessions()
    payload = (store.root / f"{handle.session_id}.json").read_text(encoding="utf-8")

    assert saved.version == 2
    assert saved.title == "inspect the project"
    assert loaded == saved
    assert loaded.restored_steps() == steps
    assert loaded.restored_quality_check() == quality
    assert '"version": 2' in payload
    assert '"title": "inspect the project"' in payload
    assert "must-not-be-persisted" not in payload
    assert list(store.root.glob("*.tmp")) == []
    assert index.invalid_count == 0
    assert index.valid_count == 1
    assert index.total_size_bytes == index.sessions[0].size_bytes
    assert index.sessions[0].size_bytes > 0
    assert index.sessions[0].round_count == 1
    assert index.sessions[0].preview == "inspect the project"


def test_atomic_replace_failure_preserves_previous_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    handle = store.new_session()
    original = store.save(handle, _messages("original"), (), None)

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(session_module.os, "replace", fail_replace)

    with pytest.raises(SessionError, match="保存失败"):
        store.save(handle, _messages("changed"), (), None)

    assert store.load(handle.session_id) == original
    assert list(store.root.glob("*.tmp")) == []


def test_listing_skips_corrupt_files_and_load_rejects_invalid_ids(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    handle = store.new_session()
    store.save(handle, _messages(), (), None)
    (store.root / "corrupt.json").write_text("not json", encoding="utf-8")
    future_id = handle.session_id[:-8] + "feedface"
    current_payload = (store.root / f"{handle.session_id}.json").read_text(
        encoding="utf-8"
    )
    future_payload = current_payload.replace('"version": 2', '"version": 3').replace(
        handle.session_id,
        future_id,
    )
    (store.root / f"{future_id}.json").write_text(future_payload, encoding="utf-8")

    index = store.list_sessions()

    assert len(index.sessions) == 1
    assert index.invalid_count == 2
    assert index.valid_count == 1
    assert index.total_size_bytes > index.sessions[0].size_bytes
    with pytest.raises(SessionError, match="ID 格式无效"):
        store.load("../outside")
    with pytest.raises(SessionError, match="无效会话"):
        store.save(
            handle,
            (Message(role="user", content="incomplete"),),
            (),
            None,
        )


def test_session_summary_and_explicit_delete_update_storage_usage(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    handle = store.new_session()
    store.save(handle, _messages(), (), None)

    summary = store.get_summary(handle.session_id)
    deleted = store.delete(handle.session_id)
    index = store.list_sessions()

    assert deleted == summary
    assert summary.size_bytes > 0
    assert not (store.root / f"{handle.session_id}.json").exists()
    assert index.valid_count == 0
    assert index.total_size_bytes == 0
    with pytest.raises(SessionError, match="未找到本地会话"):
        store.delete(handle.session_id)


def test_loads_legacy_v1_snapshot_and_migrates_on_next_save(tmp_path: Path) -> None:
    store = _store(tmp_path)
    handle = store.new_session()
    current = store.save(handle, _messages(), (), None)
    path = store.root / f"{handle.session_id}.json"
    legacy_payload = current.model_dump(mode="json")
    legacy_payload["version"] = 1
    legacy_payload.pop("title")
    import json

    path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    migrated = store.load(handle.session_id)
    rewritten = store.save(store.handle_for(migrated), migrated.messages, (), None)

    assert migrated.version == 2
    assert migrated.title == "inspect the project"
    assert rewritten.version == 2
    assert '"version": 2' in path.read_text(encoding="utf-8")


def test_session_titles_can_be_renamed_and_searched_locally(tmp_path: Path) -> None:
    store = _store(tmp_path)
    handle = store.new_session()
    saved = store.save(handle, _messages(), (), None)

    renamed = store.rename(saved.session_id, "Parser investigation")
    by_title = store.list_sessions("parser")
    by_preview = store.list_sessions("inspect")
    missing = store.list_sessions("unrelated")

    assert renamed.title == "Parser investigation"
    assert by_title.sessions[0].title == "Parser investigation"
    assert by_preview.matched_count == 1
    assert missing.valid_count == 1
    assert missing.matched_count == 0
    with pytest.raises(SessionError, match="最多"):
        store.rename(saved.session_id, "x" * (MAX_SESSION_TITLE_CHARS + 1))
    with pytest.raises(SessionError, match="控制"):
        store.list_sessions("unsafe\nquery")
