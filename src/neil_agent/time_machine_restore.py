"""Phase 3B safe checkpoint restore gates for Time Machine."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .time_machine import TimeMachineSelection, TimeMachineSnapshot


def latest_checkpoint_id(snapshot: TimeMachineSnapshot) -> str | None:
    """Return the newest task checkpoint ID in the current projection."""

    if not snapshot.checkpoints:
        return None
    return snapshot.checkpoints[-1].checkpoint_id


def can_offer_checkpoint_restore(
    snapshot: TimeMachineSnapshot,
    selection: TimeMachineSelection | None,
    *,
    busy: bool,
    pending_approvals: int,
    restore_available: bool,
) -> bool:
    """Return whether the UI may offer an explicit checkpoint restore action."""

    if not restore_available or busy or pending_approvals > 0:
        return False
    if selection is None or selection.kind != "checkpoint":
        return False
    latest_id = latest_checkpoint_id(snapshot)
    return latest_id is not None and selection.key == latest_id


def checkpoint_restore_hint(
    snapshot: TimeMachineSnapshot,
    selection: TimeMachineSelection | None,
    *,
    busy: bool,
    pending_approvals: int,
    restore_available: bool,
) -> str | None:
    """Return a short footer hint for the Time Machine detail panel."""

    if not restore_available:
        return "CHECKPOINT RESTORE UNAVAILABLE"
    if busy:
        return "CHECKPOINT RESTORE BLOCKED · TURN RUNNING"
    if pending_approvals > 0:
        return "CHECKPOINT RESTORE BLOCKED · APPROVAL PENDING"
    if selection is None or selection.kind != "checkpoint":
        return None
    latest_id = latest_checkpoint_id(snapshot)
    if latest_id is None:
        return "NO CHECKPOINT TO RESTORE"
    if selection.key != latest_id:
        return "ONLY THE LATEST CHECKPOINT CAN BE RESTORED · USE GIT FOR OLDER STATE"
    return "PRESS R TO PREVIEW AND APPROVE CHECKPOINT RESTORE"
