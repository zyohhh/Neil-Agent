"""Session revisions must prevent lost updates across independent processes."""

import multiprocessing
from pathlib import Path

import pytest

from neil_agent.errors import SessionError
from neil_agent.schemas import Message
from neil_agent.session import SessionHandle, SessionStore


def _messages(reply: str) -> tuple[Message, ...]:
    return (
        Message(role="user", content="request"),
        Message(role="assistant", content=reply),
    )


def _save_from_process(root, handle, label, ready, release, results):
    """Spawn entry point, deliberately using an identical initial revision."""
    store = SessionStore(root)
    ready.put(label)
    if not release.wait(15):
        raise RuntimeError("Concurrent writers did not start")
    try:
        store.save(handle, _messages(label), (), None)
    except SessionError:
        results.put((label, "conflict"))
    else:
        results.put((label, "saved"))


@pytest.mark.parametrize("existing", [False, True])
def test_two_processes_cannot_overwrite_the_same_revision(
    tmp_path: Path, existing: bool
) -> None:
    store = SessionStore(tmp_path)
    handle = store.new_session()
    if existing:
        handle = store.handle_for(store.save(handle, _messages("initial"), (), None))
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    release = context.Event()
    processes = [
        context.Process(
            target=_save_from_process,
            args=(tmp_path, handle, label, ready, release, results),
        )
        for label in ("one", "two")
    ]
    try:
        for process in processes:
            process.start()
        assert {ready.get(timeout=15), ready.get(timeout=15)} == {"one", "two"}
        release.set()
        outcomes = dict(results.get(timeout=15) for _ in processes)
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        assert sorted(outcomes.values()) == ["conflict", "saved"]
        winner = next(label for label, status in outcomes.items() if status == "saved")
        assert store.load(handle.session_id).messages[-1].content == winner
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        ready.close()
        results.close()


def test_stale_handles_cannot_restore_deleted_or_renamed_state(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    saved = store.save(store.new_session(), _messages("original"), (), None)
    handle = store.handle_for(saved)
    renamed = store.rename(
        saved.session_id, "new title", expected_digest=handle.base_digest
    )
    with pytest.raises(SessionError, match="修改"):
        store.save(handle, _messages("stale"), (), None)
    assert store.load(saved.session_id) == renamed
    store.delete(saved.session_id)
    with pytest.raises(SessionError, match="修改或删除"):
        store.save(store.handle_for(renamed), _messages("stale"), (), None)
    assert not store.has_saved(saved.session_id)


def test_rename_and_approved_delete_reject_changed_revision(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    saved = store.save(store.new_session(), _messages("original"), (), None)
    summary = store.get_summary(saved.session_id)
    current = store.save(store.handle_for(saved), _messages("new reply"), (), None)
    with pytest.raises(SessionError, match="修改"):
        store.rename(saved.session_id, "stale title", expected_digest=summary.revision)
    with pytest.raises(SessionError, match="确认后"):
        store.delete(saved.session_id, expected_digest=summary.revision)
    assert store.load(saved.session_id) == current


def test_new_identity_cannot_overwrite_an_existing_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    handle = store.new_session()
    saved = store.save(handle, _messages("original"), (), None)
    with pytest.raises(SessionError, match="已存在"):
        store.save(
            SessionHandle(session_id=handle.session_id, created_at=handle.created_at),
            _messages("lost"),
            (),
            None,
        )
    assert store.load(handle.session_id) == saved


def test_lock_timeout_preserves_saved_state(tmp_path: Path) -> None:
    from neil_agent.file_lock import StateFileLock

    store = SessionStore(tmp_path)
    saved = store.save(store.new_session(), _messages("original"), (), None)
    with StateFileLock(store.root / ".sessions.lock", SessionError):
        with pytest.raises(SessionError, match="其他进程"):
            with StateFileLock(
                store.root / ".sessions.lock", SessionError, timeout=0.02
            ):
                pytest.fail("Lock must be exclusive")
    assert store.load(saved.session_id) == saved
