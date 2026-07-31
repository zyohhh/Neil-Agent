"""Tests for bounded, one-use non-interactive approval records."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

from neil_agent.approval import (
    ApprovalBinding,
    ApprovalRequest,
    ApprovalStore,
    NoninteractiveApprovalBroker,
)
from neil_agent.errors import ApprovalError
from neil_agent.schemas import ToolCall


def _call(*, content: str = "new content") -> ToolCall:
    return ToolCall(
        id="call-1",
        name="write_file",
        arguments={"path": "notes.txt", "content": content},
    )


def test_approval_request_stores_hashes_without_hidden_input_values(
    tmp_path: Path,
) -> None:
    store = ApprovalStore(tmp_path)

    request = store.create(
        _call(content="PRIVATE-ARGUMENT"),
        "exact safe preview",
        prompt="PRIVATE-PROMPT",
        instructions="PRIVATE-INSTRUCTIONS",
    )

    payload_path = (
        tmp_path
        / ".neil-agent"
        / "approvals"
        / "pending"
        / f"{request.request_id}.json"
    )
    payload = payload_path.read_text(encoding="utf-8")
    parsed = json.loads(payload)
    assert "PRIVATE-ARGUMENT" not in payload
    assert "PRIVATE-PROMPT" not in payload
    assert "PRIVATE-INSTRUCTIONS" not in payload
    assert parsed["preview"] == "exact safe preview"
    assert len(parsed["arguments_sha256"]) == 64
    assert parsed["version"] == 2
    assert parsed["binding_kind"] == "generic-tool"
    assert parsed["binding_version"] == 1
    assert len(parsed["binding_sha256"]) == 64


def test_v2_binding_fields_are_required_in_persisted_schema(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    request = store.create(
        _call(),
        "preview",
        prompt="update notes",
        instructions="root rules",
    )
    payload = request.model_dump(mode="json")

    for field in ("binding_kind", "binding_version", "binding_sha256"):
        incomplete = {name: value for name, value in payload.items() if name != field}
        with pytest.raises(ValidationError):
            ApprovalRequest.model_validate(incomplete)


def test_matching_approval_is_consumed_once_before_execution(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    call = _call()
    request = store.create(
        call,
        "preview",
        prompt="update notes",
        instructions="root rules",
    )

    store.consume(
        request,
        call,
        "preview",
        prompt="update notes",
        instructions="root rules",
    )

    consumed_path = (
        tmp_path
        / ".neil-agent"
        / "approvals"
        / "consumed"
        / f"{request.request_id}.json"
    )
    assert consumed_path.is_file()
    with pytest.raises(ApprovalError, match="已经使用"):
        store.load(request.approval_id)


def test_changed_operation_does_not_match_approval(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    request = store.create(
        _call(),
        "preview",
        prompt="update notes",
        instructions="root rules",
    )

    assert (
        store.matches(
            request,
            _call(content="different"),
            "preview",
            prompt="update notes",
            instructions="root rules",
        )
        is False
    )
    assert store.load(request.approval_id) == request


def test_binding_mismatch_burns_approval_permanently(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    original_binding = ApprovalBinding(
        kind="sandbox-run-command",
        version=1,
        sha256="1" * 64,
    )
    request = store.create(
        _call(),
        "preview",
        prompt="update notes",
        instructions="root rules",
        binding=original_binding,
    )
    changed_binding = original_binding.model_copy(update={"sha256": "2" * 64})

    with pytest.raises(ApprovalError, match="永久失效"):
        store.consume(
            request,
            _call(),
            "preview",
            prompt="update notes",
            instructions="root rules",
            binding=changed_binding,
        )

    with pytest.raises(ApprovalError, match="已经使用"):
        store.load(request.approval_id)


def test_approve_broker_claims_before_call_and_mismatch_cannot_be_restored(
    tmp_path: Path,
) -> None:
    store = ApprovalStore(tmp_path)
    request = store.create(
        _call(),
        "preview",
        prompt="update notes",
        instructions="root rules",
    )
    generated: list[ApprovalRequest] = []
    broker = NoninteractiveApprovalBroker(
        store,
        mode="approve",
        prompt="update notes",
        instructions=lambda: "root rules",
        request_handler=generated.append,
        approval_id=request.approval_id,
    )

    with pytest.raises(ApprovalError, match="已经使用"):
        store.load(request.approval_id)
    assert broker(_call(content="different"), "different preview") is False
    assert len(generated) == 1
    assert generated[0].approval_id != request.approval_id

    assert broker(_call(), "preview") is False
    assert broker.consumed_request_id is None


def test_approve_prompt_mismatch_is_claimed_and_permanently_invalid(
    tmp_path: Path,
) -> None:
    store = ApprovalStore(tmp_path)
    request = store.create(
        _call(),
        "preview",
        prompt="original prompt",
        instructions="root rules",
    )

    with pytest.raises(ApprovalError, match="prompt.*永久失效"):
        NoninteractiveApprovalBroker(
            store,
            mode="approve",
            prompt="different prompt",
            instructions=lambda: "root rules",
            request_handler=lambda _request: None,
            approval_id=request.approval_id,
        )
    with pytest.raises(ApprovalError, match="已经使用"):
        store.load(request.approval_id)


def test_two_process_like_claims_allow_only_one_winner(
    tmp_path: Path,
) -> None:
    creator = ApprovalStore(tmp_path)
    request = creator.create(
        _call(),
        "preview",
        prompt="update notes",
        instructions="root rules",
    )
    stores = (ApprovalStore(tmp_path), ApprovalStore(tmp_path))
    barrier = Barrier(2)

    def try_claim(store: ApprovalStore) -> ApprovalRequest | ApprovalError:
        barrier.wait(timeout=5)
        try:
            return store.claim(request.approval_id, prompt="update notes")
        except ApprovalError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(try_claim, stores))

    assert sum(isinstance(outcome, ApprovalRequest) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, ApprovalError) for outcome in outcomes) == 1
    with pytest.raises(ApprovalError, match="已经使用"):
        creator.load(request.approval_id)


def test_claim_burns_id_before_a_tampered_pending_record_is_read(
    tmp_path: Path,
) -> None:
    store = ApprovalStore(tmp_path)
    request = store.create(
        _call(),
        "preview",
        prompt="update notes",
        instructions="root rules",
    )
    pending_path = (
        tmp_path
        / ".neil-agent"
        / "approvals"
        / "pending"
        / f"{request.request_id}.json"
    )
    original = pending_path.read_bytes()
    payload = json.loads(original)
    payload["preview"] = "temporary tamper"
    pending_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ApprovalError, match="用户确认"):
        store.claim(request.approval_id, prompt="update notes")

    pending_path.write_bytes(original)
    with pytest.raises(ApprovalError, match="已经存在|消费|使用"):
        store.claim(request.approval_id, prompt="update notes")


def test_claim_cleanup_failure_still_blocks_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ApprovalStore(tmp_path)
    request = store.create(
        _call(),
        "preview",
        prompt="update notes",
        instructions="root rules",
    )
    pending_path = (
        tmp_path
        / ".neil-agent"
        / "approvals"
        / "pending"
        / f"{request.request_id}.json"
    )
    original_unlink = Path.unlink

    def failing_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == pending_path:
            raise OSError("simulated cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(ApprovalError, match="仍不可重放"):
        store.claim(request.approval_id, prompt="update notes")
    with pytest.raises(ApprovalError, match="已经使用"):
        store.load(request.approval_id)


def test_old_consumed_markers_are_pruned_before_a_new_claim(
    tmp_path: Path,
) -> None:
    current_time = datetime(2026, 7, 23, tzinfo=timezone.utc)

    def clock() -> datetime:
        return current_time

    store = ApprovalStore(tmp_path, clock=clock)
    first = store.create(
        _call(),
        "first preview",
        prompt="update notes",
        instructions="root rules",
    )
    store.claim(first.approval_id, prompt="update notes")
    first_marker = (
        tmp_path / ".neil-agent" / "approvals" / "consumed" / f"{first.request_id}.json"
    )
    assert first_marker.is_file()

    current_time += timedelta(days=2)
    second = store.create(
        _call(content="second"),
        "second preview",
        prompt="update notes",
        instructions="root rules",
    )
    store.claim(second.approval_id, prompt="update notes")

    assert not first_marker.exists()


def test_legacy_v1_pending_record_is_invalidated_without_blocking_create(
    tmp_path: Path,
) -> None:
    pending = tmp_path / ".neil-agent" / "approvals" / "pending"
    pending.mkdir(parents=True)
    request_id = "a" * 32
    legacy_path = pending / f"{request_id}.json"
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "request_id": request_id,
                "created_at": "2026-07-23T00:00:00Z",
                "expires_at": "2026-07-23T00:15:00Z",
                "workspace": str(tmp_path.resolve()),
                "prompt_sha256": "1" * 64,
                "instructions_sha256": "2" * 64,
                "tool_name": "write_file",
                "arguments_sha256": "3" * 64,
                "preview_sha256": "4" * 64,
                "preview": "legacy preview",
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    store = ApprovalStore(tmp_path)

    with pytest.raises(ApprovalError, match="旧版"):
        store.load(f"{request_id}.{'f' * 64}")

    replacement = store.create(
        _call(),
        "replacement preview",
        prompt="update notes",
        instructions="root rules",
    )
    assert not legacy_path.exists()
    assert store.load(replacement.approval_id) == replacement


def test_caller_visible_approval_id_detects_record_tampering(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    request = store.create(
        _call(),
        "preview",
        prompt="update notes",
        instructions="root rules",
    )
    payload_path = (
        tmp_path
        / ".neil-agent"
        / "approvals"
        / "pending"
        / f"{request.request_id}.json"
    )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["preview"] = "tampered preview"
    payload["preview_sha256"] = sha256(b"tampered preview").hexdigest()
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ApprovalError, match="用户确认"):
        store.load(request.approval_id)


def test_expired_or_prompt_mismatched_approval_fails_closed(tmp_path: Path) -> None:
    created_at = datetime(2026, 7, 23, tzinfo=timezone.utc)
    current_time = created_at

    def clock() -> datetime:
        return current_time

    store = ApprovalStore(tmp_path, clock=clock)
    request = store.create(
        _call(),
        "preview",
        prompt="update notes",
        instructions="root rules",
    )

    with pytest.raises(ApprovalError, match="prompt"):
        store.preflight(request.approval_id, prompt="different prompt")

    current_time = created_at + timedelta(minutes=16)
    with pytest.raises(ApprovalError, match="过期"):
        store.load(request.approval_id)
    replacement = store.create(
        _call(content="replacement"),
        "replacement preview",
        prompt="update notes",
        instructions="root rules",
    )
    old_path = (
        tmp_path
        / ".neil-agent"
        / "approvals"
        / "pending"
        / f"{request.request_id}.json"
    )
    assert not old_path.exists()
    assert store.load(replacement.approval_id) == replacement


def test_approval_preview_rejects_terminal_control_characters(
    tmp_path: Path,
) -> None:
    store = ApprovalStore(tmp_path)

    with pytest.raises(ApprovalError, match="记录写入|预览|格式"):
        store.create(
            _call(),
            "safe\u202eunsafe",
            prompt="update",
            instructions="",
        )
