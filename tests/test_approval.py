"""Tests for bounded, one-use non-interactive approval records."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from neil_agent.approval import ApprovalStore
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
