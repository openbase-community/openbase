"""Short-lived, account-scoped inbound call invitation state."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from openbase_coder_cli.livekit_agent.codex_thread_state import (
    thread_state_file_lock,
)
from openbase_coder_cli.paths import OPENBASE_BASE_DIR

INVITATION_SCHEMA_VERSION = 1
INVITATION_TTL_SECONDS = 60
INVITATION_RETENTION_SECONDS = 300
INVITATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
ROOM_NAME_RE = re.compile(r"^room-inbound-[0-9a-f]{24}$")
STATE_PATH = OPENBASE_BASE_DIR / "inbound-call-invitations.json"
VALID_STATUSES = {"pending", "answered", "declined", "activated"}


class InboundCallInvitationError(ValueError):
    """An inbound call invitation is missing, stale, or not usable."""


class InboundCallInvitationExpired(InboundCallInvitationError):
    """An inbound call invitation expired before it was answered."""


class InboundCallInvitationConflict(InboundCallInvitationError):
    """An inbound call invitation cannot perform the requested transition."""


@dataclass(frozen=True, slots=True)
class InboundCallInvitation:
    invitation_id: str
    account_identity: str
    caller_name: str
    thread_id: str
    cwd: str
    agent_name: str
    room_name: str
    livekit_dispatch_agent_name: str
    created_at: float
    expires_at: float
    status: str


def new_invitation_id() -> str:
    invitation_id = secrets.token_urlsafe(32)
    if not INVITATION_ID_RE.fullmatch(invitation_id):
        raise RuntimeError("Unable to generate a valid inbound call invitation ID.")
    return invitation_id


def create_invitation(
    *,
    invitation_id: str,
    account_identity: str,
    caller_name: str,
    thread_id: str,
    cwd: str,
    agent_name: str,
    livekit_dispatch_agent_name: str,
    now: float | None = None,
) -> InboundCallInvitation:
    timestamp = now if now is not None else time.time()
    invitation = InboundCallInvitation(
        invitation_id=invitation_id,
        account_identity=account_identity,
        caller_name=caller_name,
        thread_id=thread_id,
        cwd=cwd,
        agent_name=agent_name,
        room_name=f"room-inbound-{secrets.token_hex(12)}",
        livekit_dispatch_agent_name=livekit_dispatch_agent_name,
        created_at=timestamp,
        expires_at=timestamp + INVITATION_TTL_SECONDS,
        status="pending",
    )
    _validate_invitation(invitation)
    def add(invitations: dict[str, InboundCallInvitation]) -> None:
        if invitation_id in invitations:
            raise InboundCallInvitationConflict(
                "Inbound call invitation ID is already in use."
            )
        invitations[invitation_id] = invitation

    _mutate(add, now=timestamp)
    return invitation


def set_cloud_expiry(
    invitation_id: str,
    *,
    account_identity: str,
    expires_at: int,
    now: float | None = None,
) -> InboundCallInvitation:
    timestamp = now if now is not None else time.time()
    if expires_at <= timestamp or expires_at > timestamp + INVITATION_TTL_SECONDS + 5:
        raise InboundCallInvitationConflict(
            "Cloud returned an invalid inbound call expiry."
        )

    updated: InboundCallInvitation | None = None

    def apply(invitations: dict[str, InboundCallInvitation]) -> None:
        nonlocal updated
        current = _owned(invitation_id, account_identity, invitations)
        if current.status != "pending":
            raise InboundCallInvitationConflict("Invitation is no longer pending.")
        updated = InboundCallInvitation(**{**asdict(current), "expires_at": expires_at})
        invitations[invitation_id] = updated

    _mutate(apply, now=timestamp)
    if updated is None:
        raise InboundCallInvitationError("Inbound call invitation was not found.")
    return updated


def remove_pending_invitation(
    invitation_id: str,
    *,
    account_identity: str,
) -> None:
    def apply(invitations: dict[str, InboundCallInvitation]) -> None:
        current = invitations.get(invitation_id)
        if current and current.account_identity == account_identity and current.status == "pending":
            invitations.pop(invitation_id, None)

    _mutate(apply)


def answer_invitation(
    invitation_id: str,
    *,
    account_identity: str,
    now: float | None = None,
) -> InboundCallInvitation:
    return _transition(
        invitation_id,
        account_identity=account_identity,
        allowed={"pending", "answered"},
        status="answered",
        now=now,
    )


def decline_invitation(
    invitation_id: str,
    *,
    account_identity: str,
    now: float | None = None,
) -> InboundCallInvitation:
    return _transition(
        invitation_id,
        account_identity=account_identity,
        allowed={"pending", "declined"},
        status="declined",
        now=now,
    )


def invitation_for_activation(
    invitation_id: str,
    *,
    account_identity: str,
    room_name: str,
    now: float | None = None,
) -> InboundCallInvitation:
    timestamp = now if now is not None else time.time()
    with thread_state_file_lock(STATE_PATH):
        invitations = _read_state(timestamp)
        invitation = _owned(invitation_id, account_identity, invitations)
        _require_fresh(invitation, timestamp)
        if invitation.status == "activated":
            return invitation
        if invitation.status != "answered" or invitation.room_name != room_name:
            raise InboundCallInvitationConflict(
                "Inbound call invitation is not ready for route activation."
            )
        return invitation


def mark_invitation_activated(
    invitation_id: str,
    *,
    account_identity: str,
    now: float | None = None,
) -> InboundCallInvitation:
    return _transition(
        invitation_id,
        account_identity=account_identity,
        allowed={"answered", "activated"},
        status="activated",
        now=now,
    )


def _transition(
    invitation_id: str,
    *,
    account_identity: str,
    allowed: set[str],
    status: str,
    now: float | None,
) -> InboundCallInvitation:
    timestamp = now if now is not None else time.time()
    updated: InboundCallInvitation | None = None

    def apply(invitations: dict[str, InboundCallInvitation]) -> None:
        nonlocal updated
        current = _owned(invitation_id, account_identity, invitations)
        _require_fresh(current, timestamp)
        if current.status not in allowed:
            raise InboundCallInvitationConflict(
                f"Inbound call invitation is already {current.status}."
            )
        updated = InboundCallInvitation(**{**asdict(current), "status": status})
        invitations[invitation_id] = updated

    _mutate(apply, now=timestamp)
    if updated is None:
        raise InboundCallInvitationError("Inbound call invitation was not found.")
    return updated


def _mutate(
    mutation: Callable[[dict[str, InboundCallInvitation]], None],
    *,
    now: float | None = None,
) -> None:
    timestamp = now if now is not None else time.time()
    with thread_state_file_lock(STATE_PATH):
        invitations = _read_state(timestamp)
        mutation(invitations)
        _write_state(invitations)


def _owned(
    invitation_id: str,
    account_identity: str,
    invitations: dict[str, InboundCallInvitation],
) -> InboundCallInvitation:
    if not INVITATION_ID_RE.fullmatch(invitation_id):
        raise InboundCallInvitationError("Inbound call invitation is invalid.")
    invitation = invitations.get(invitation_id)
    if invitation is None or invitation.account_identity != account_identity:
        raise InboundCallInvitationError("Inbound call invitation was not found.")
    return invitation


def _require_fresh(invitation: InboundCallInvitation, now: float) -> None:
    if invitation.expires_at <= now:
        raise InboundCallInvitationExpired("Inbound call invitation has expired.")


def _read_state(now: float) -> dict[str, InboundCallInvitation]:
    if not STATE_PATH.is_file() or STATE_PATH.is_symlink():
        return {}
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != INVITATION_SCHEMA_VERSION:
        return {}
    raw_invitations = payload.get("invitations")
    if not isinstance(raw_invitations, dict):
        return {}
    invitations: dict[str, InboundCallInvitation] = {}
    for invitation_id, raw in raw_invitations.items():
        invitation = _invitation_from_payload(invitation_id, raw)
        if invitation and invitation.expires_at + INVITATION_RETENTION_SECONDS > now:
            invitations[invitation_id] = invitation
    return invitations


def _invitation_from_payload(
    invitation_id: Any,
    raw: Any,
) -> InboundCallInvitation | None:
    if not isinstance(invitation_id, str) or not isinstance(raw, dict):
        return None
    try:
        invitation = InboundCallInvitation(invitation_id=invitation_id, **raw)
        _validate_invitation(invitation)
    except (TypeError, ValueError):
        return None
    return invitation


def _validate_invitation(invitation: InboundCallInvitation) -> None:
    string_values = (
        invitation.account_identity,
        invitation.caller_name,
        invitation.thread_id,
        invitation.cwd,
        invitation.agent_name,
        invitation.livekit_dispatch_agent_name,
    )
    if (
        not INVITATION_ID_RE.fullmatch(invitation.invitation_id)
        or not ROOM_NAME_RE.fullmatch(invitation.room_name)
        or any(not value for value in string_values)
        or invitation.status not in VALID_STATUSES
        or not isinstance(invitation.created_at, int | float)
        or not isinstance(invitation.expires_at, int | float)
        or invitation.expires_at <= invitation.created_at
    ):
        raise ValueError("Invalid inbound call invitation state.")


def _write_state(invitations: dict[str, InboundCallInvitation]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_PATH.parent.chmod(0o700)
    payload = {
        "schema_version": INVITATION_SCHEMA_VERSION,
        "invitations": {
            key: {
                field: value
                for field, value in asdict(invitation).items()
                if field != "invitation_id"
            }
            for key, invitation in sorted(invitations.items())
        },
    }
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".inbound-call-invitations-",
        dir=STATE_PATH.parent,
        text=True,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(body)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, STATE_PATH)
        STATE_PATH.chmod(0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise
