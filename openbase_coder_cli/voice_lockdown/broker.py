"""Persistent challenge broker with atomic, one-use approval leases."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from super_agents.execution_control import ApprovalAuthorizationRequest

from openbase_coder_cli.paths import OPENBASE_BASE_DIR

from .audit import LockdownAuditLog
from .keychain import (
    KeychainCorruptError,
    KeychainRecord,
    KeychainUnavailableError,
    MacOSKeychain,
)
from .policy import BaselineCheck, check_managed_mcp_registration, check_safe_baseline
from .verifier import verify_phrase

CHALLENGE_TTL_SECONDS = 120.0
CAPABILITY_TTL_SECONDS = 30.0
MAX_ATTEMPTS = 3
COOLDOWN_SECONDS = 300.0
DEFAULT_DB_PATH = OPENBASE_BASE_DIR / "security" / "voice-lockdown.sqlite3"


class LockdownDeniedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalScope:
    backend: str
    request_id: str
    thread_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    action_digest: str
    room_sid: str
    participant_identity: str


def canonical_action_digest(action: Any) -> str:
    encoded = json.dumps(
        action,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class VoiceLockdownBroker:
    def __init__(
        self,
        *,
        db_path: str | Path = DEFAULT_DB_PATH,
        keychain: MacOSKeychain | None = None,
        clock: Any = time.time,
    ) -> None:
        self.db_path = Path(db_path)
        self.keychain = keychain or MacOSKeychain()
        self.clock = clock
        self._lock = threading.RLock()
        self._initialize_db()
        self.audit = LockdownAuditLog(connect=self._connect, clock=self.clock, lock=self._lock)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_db(self) -> None:
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.db_path.parent, 0o700)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS challenges (
                    id TEXT PRIMARY KEY,
                    scope_json TEXT NOT NULL,
                    action_summary TEXT NOT NULL,
                    challenge_mac TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    attempts_remaining INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    capability_id TEXT,
                    capability_mac TEXT,
                    capability_expires_at REAL,
                    consumed_at REAL
                );
                CREATE TABLE IF NOT EXISTS cooldowns (
                    participant_hash TEXT PRIMARY KEY,
                    until_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    created_at REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    previous_mac TEXT NOT NULL,
                    event_mac TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(challenges)")}
            if "capability_id" not in columns:
                db.execute("ALTER TABLE challenges ADD COLUMN capability_id TEXT")
            if "capability_mac" not in columns:
                db.execute("ALTER TABLE challenges ADD COLUMN capability_mac TEXT")
            if "challenge_mac" not in columns:
                db.execute("ALTER TABLE challenges ADD COLUMN challenge_mac TEXT")
        os.chmod(self.db_path, 0o600)

    def _record(self) -> KeychainRecord | None:
        return self.keychain.read()

    def health(self) -> tuple[str, KeychainRecord | None]:
        try:
            with self._connect() as db:
                configured = db.execute(
                    "SELECT value FROM metadata WHERE key = 'configured'"
                ).fetchone()
        except sqlite3.DatabaseError:
            return "indeterminate", None
        if configured is None and platform.system() != "Darwin":
            return "unconfigured", None
        try:
            record = self._record()
        except (KeychainUnavailableError, KeychainCorruptError):
            return "indeterminate", None
        if record is not None:
            if configured is None or not self.audit.integrity_valid(record):
                return "indeterminate", None
            return ("ready" if record.enabled else "disabled"), record
        return ("indeterminate" if configured else "unconfigured"), None

    def status(self) -> dict[str, Any]:
        health, record = self.health()
        baseline = check_safe_baseline()
        managed = check_managed_mcp_registration()
        now = self.clock()
        with self._connect() as db:
            challenge = db.execute(
                """SELECT * FROM challenges
                WHERE state IN ('awaiting_phrase', 'ready') AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1""",
                (now,),
            ).fetchone()
        return {
            "enabled": bool(record and record.enabled),
            "health": health,
            "baselineSafe": baseline.safe,
            "baselineReasons": list(baseline.reasons),
            "managedControlsReady": managed.ready,
            "managedControlReasons": list(managed.reasons),
            "challenge": self._public_challenge(challenge) if challenge else None,
        }

    def set_configuration(self, record: KeychainRecord, *, event_type: str) -> None:
        self.keychain.write(record)
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('configured', '1')"
            )
        self.audit.append(event_type, {"enabled": record.enabled}, record=record)

    def require_enabled(self) -> KeychainRecord:
        health, record = self.health()
        if health != "ready" or record is None or not record.enabled:
            raise LockdownDeniedError(f"Voice lockdown is not safely available ({health}).")
        baseline = check_safe_baseline()
        if not baseline.safe:
            self.audit.append("baseline_unsafe", {"reasons": list(baseline.reasons)}, record=record)
            raise LockdownDeniedError("Configured coding backend baseline is unsafe.")
        return record

    def create_challenge(self, scope: ApprovalScope, *, action_summary: str) -> dict[str, Any]:
        record = self.require_enabled()
        now = self.clock()
        participant_hash = self._participant_hash(scope)
        with self._lock, self._connect() as db:
            cooldown = db.execute(
                "SELECT until_at FROM cooldowns WHERE participant_hash = ?",
                (participant_hash,),
            ).fetchone()
            if cooldown and float(cooldown["until_at"]) > now:
                raise LockdownDeniedError("Safe-phrase verification is temporarily rate limited.")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE challenges SET state = 'revoked' WHERE state IN ('awaiting_phrase', 'ready')"
            )
            challenge_id = f"vlc_{uuid.uuid4().hex}"
            scope_json = json.dumps(asdict(scope), separators=(",", ":"), sort_keys=True)
            summary = action_summary[:240]
            expires_at = now + CHALLENGE_TTL_SECONDS
            challenge_mac = self._challenge_mac(
                challenge_id,
                scope_json,
                summary,
                expires_at,
                record,
            )
            db.execute(
                """INSERT INTO challenges(
                    id, scope_json, action_summary, challenge_mac, created_at, expires_at,
                    attempts_remaining, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_phrase')""",
                (
                    challenge_id,
                    scope_json,
                    summary,
                    challenge_mac,
                    now,
                    expires_at,
                    MAX_ATTEMPTS,
                ),
            )
            db.commit()
        self.audit.append("challenge_created", self._audit_scope(scope), record=record)
        return self.challenge(challenge_id)

    def challenge(self, challenge_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM challenges WHERE id = ?", (challenge_id,)).fetchone()
        if row is None:
            raise LockdownDeniedError("Voice-lockdown challenge was not found.")
        return self._public_challenge(row)

    def is_armed(self, *, room_sid: str, participant_identity: str) -> bool:
        """Return whether transcript events for this exact caller must be suppressed."""
        health, record = self.health()
        if health != "ready" or record is None or not record.enabled:
            return False
        now = self.clock()
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM challenges WHERE state = 'awaiting_phrase' AND expires_at > ?",
                (now,),
            ).fetchall()
        return any(
            self._challenge_valid(row, record)
            and (scope := ApprovalScope(**json.loads(row["scope_json"]))).room_sid == room_sid
            and scope.participant_identity == participant_identity
            for row in rows
        )

    def verify_final_utterance(
        self,
        transcript: str,
        *,
        room_sid: str,
        participant_identity: str,
    ) -> str:
        """Consume one whole final utterance and return a non-secret outcome."""
        record = self.require_enabled()
        now = self.clock()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT * FROM challenges
                WHERE state = 'awaiting_phrase' AND expires_at > ?
                ORDER BY created_at DESC""",
                (now,),
            ).fetchall()
            row = next(
                (
                    item
                    for item in rows
                    if self._challenge_valid(item, record)
                    and self._scope_from_row(item).room_sid == room_sid
                    and self._scope_from_row(item).participant_identity == participant_identity
                ),
                None,
            )
            if row is None:
                db.rollback()
                return "not_armed"
            scope = self._scope_from_row(row)
            if verify_phrase(transcript, salt_b64=record.salt, verifier_b64=record.verifier):
                capability_id = secrets.token_urlsafe(32)
                capability_expires_at = now + CAPABILITY_TTL_SECONDS
                capability_mac = self._capability_mac(
                    capability_id,
                    row["scope_json"],
                    capability_expires_at,
                    record,
                )
                db.execute(
                    """UPDATE challenges SET state = 'ready', capability_id = ?, capability_mac = ?,
                    capability_expires_at = ? WHERE id = ? AND state = 'awaiting_phrase'""",
                    (capability_id, capability_mac, capability_expires_at, row["id"]),
                )
                db.commit()
                self.audit.append("capability_issued", self._audit_scope(scope), record=record)
                return "authorized"
            attempts = int(row["attempts_remaining"]) - 1
            new_state = "rate_limited" if attempts <= 0 else "awaiting_phrase"
            db.execute(
                "UPDATE challenges SET attempts_remaining = ?, state = ? WHERE id = ?",
                (max(0, attempts), new_state, row["id"]),
            )
            if attempts <= 0:
                db.execute(
                    "INSERT OR REPLACE INTO cooldowns(participant_hash, until_at) VALUES(?, ?)",
                    (self._participant_hash(scope), now + COOLDOWN_SECONDS),
                )
            db.commit()
        self.audit.append(
            "phrase_mismatch" if attempts > 0 else "rate_limited",
            {**self._audit_scope(scope), "attemptsRemaining": max(0, attempts)},
            record=record,
        )
        return "mismatch" if attempts > 0 else "rate_limited"

    def consume_capability(self, scope: ApprovalScope) -> bool:
        """Atomically consume the only ready capability matching exact scope."""
        try:
            record = self.require_enabled()
        except LockdownDeniedError:
            return False
        now = self.clock()
        scope_json = json.dumps(asdict(scope), separators=(",", ":"), sort_keys=True)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT id FROM challenges WHERE scope_json = ? AND state = 'ready'
                AND capability_expires_at > ? ORDER BY created_at DESC LIMIT 1""",
                (scope_json, now),
            ).fetchone()
            if row is None:
                db.rollback()
                self.audit.append("authorization_denied", self._audit_scope(scope), record=record)
                return False
            full_row = db.execute("SELECT * FROM challenges WHERE id = ?", (row["id"],)).fetchone()
            if full_row is None or not self._capability_valid(full_row, record):
                db.rollback()
                self.audit.append("authorization_denied", self._audit_scope(scope), record=record)
                return False
            updated = db.execute(
                """UPDATE challenges SET state = 'consumed', consumed_at = ?
                WHERE id = ? AND state = 'ready' AND capability_expires_at > ?""",
                (now, row["id"], now),
            ).rowcount
            db.commit()
        if updated == 1:
            self.audit.append("capability_consumed", self._audit_scope(scope), record=record)
            return True
        return False

    def consume_authorization(self, request: ApprovalAuthorizationRequest) -> bool:
        """Consume a product-bound lease for one generic backend request."""
        try:
            record = self.require_enabled()
        except LockdownDeniedError:
            return False
        now = self.clock()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT * FROM challenges WHERE state = 'ready'
                AND capability_expires_at > ? ORDER BY created_at DESC""",
                (now,),
            ).fetchall()
            row = next(
                (
                    item
                    for item in rows
                    if self._generic_scope_matches(self._scope_from_row(item), request)
                ),
                None,
            )
            if row is None:
                db.rollback()
                return False
            if not self._capability_valid(row, record):
                db.rollback()
                return False
            changed = db.execute(
                """UPDATE challenges SET state = 'consumed', consumed_at = ?
                WHERE id = ? AND state = 'ready' AND capability_expires_at > ?""",
                (now, row["id"], now),
            ).rowcount
            db.commit()
        if changed == 1:
            self.audit.append("capability_consumed", self._audit_scope(self._scope_from_row(row)), record=record)
            return True
        return False

    def cancel_challenge(self, challenge_id: str) -> bool:
        record = self.require_enabled()
        with self._connect() as db:
            changed = db.execute(
                "UPDATE challenges SET state = 'revoked' WHERE id = ? AND state IN ('awaiting_phrase', 'ready')",
                (challenge_id,),
            ).rowcount
        if changed:
            self.audit.append("challenge_revoked", {"challengeHash": self._hash_id(challenge_id)}, record=record)
        return changed == 1

    def revoke_all(self, *, reason: str) -> None:
        health, record = self.health()
        with self._connect() as db:
            db.execute("UPDATE challenges SET state = 'revoked' WHERE state IN ('awaiting_phrase', 'ready')")
        if record is not None:
            self.audit.append("capabilities_revoked", {"reason": reason[:80], "health": health}, record=record)

    def recent_audit(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.audit.recent(limit=limit)

    @staticmethod
    def _scope_from_row(row: sqlite3.Row) -> ApprovalScope:
        return ApprovalScope(**json.loads(row["scope_json"]))

    @staticmethod
    def _public_challenge(row: sqlite3.Row) -> dict[str, Any]:
        scope = VoiceLockdownBroker._scope_from_row(row)
        return {
            "id": row["id"],
            "state": row["state"],
            "expiresAt": row["expires_at"],
            "capabilityExpiresAt": row["capability_expires_at"],
            "attemptsRemaining": row["attempts_remaining"],
            "backend": scope.backend,
            "requestId": scope.request_id,
            "threadId": scope.thread_id,
            "turnId": scope.turn_id,
            "toolName": scope.tool_name,
            "actionSummary": row["action_summary"],
        }

    @staticmethod
    def _hash_id(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:16]

    def _participant_hash(self, scope: ApprovalScope) -> str:
        return self._hash_id(f"{scope.room_sid}\0{scope.participant_identity}")

    def _audit_scope(self, scope: ApprovalScope) -> dict[str, Any]:
        return {
            "backend": scope.backend,
            "requestHash": self._hash_id(scope.request_id),
            "threadHash": self._hash_id(scope.thread_id),
            "turnHash": self._hash_id(scope.turn_id),
            "toolCallHash": self._hash_id(scope.tool_call_id),
            "toolName": scope.tool_name,
            "actionDigest": scope.action_digest,
            "roomHash": self._hash_id(scope.room_sid),
            "participantHash": self._hash_id(scope.participant_identity),
        }

    @staticmethod
    def _generic_scope_matches(scope: ApprovalScope, request: ApprovalAuthorizationRequest) -> bool:
        return (
            scope.backend == request.backend
            and scope.request_id == request.request_id
            and scope.thread_id == (request.thread_id or "")
            and scope.turn_id == (request.turn_id or "")
            and scope.tool_call_id == (request.tool_call_id or "")
            and scope.tool_name == request.action_type
            and scope.action_digest == request.action_digest
        )

    @staticmethod
    def _capability_mac(
        capability_id: str,
        scope_json: str,
        expires_at: float,
        record: KeychainRecord,
    ) -> str:
        payload = f"{capability_id}|{scope_json}|{expires_at:.6f}".encode()
        return hmac.new(base64.b64decode(record.audit_key), payload, hashlib.sha256).hexdigest()

    @staticmethod
    def _challenge_mac(
        challenge_id: str,
        scope_json: str,
        action_summary: str,
        expires_at: float,
        record: KeychainRecord,
    ) -> str:
        payload = f"{challenge_id}|{scope_json}|{action_summary}|{expires_at:.6f}".encode()
        return hmac.new(base64.b64decode(record.audit_key), payload, hashlib.sha256).hexdigest()

    @classmethod
    def _challenge_valid(cls, row: sqlite3.Row, record: KeychainRecord) -> bool:
        challenge_mac = row["challenge_mac"]
        if not isinstance(challenge_mac, str):
            return False
        expected = cls._challenge_mac(
            row["id"],
            row["scope_json"],
            row["action_summary"],
            float(row["expires_at"]),
            record,
        )
        return hmac.compare_digest(expected, challenge_mac)

    @classmethod
    def _capability_valid(cls, row: sqlite3.Row, record: KeychainRecord) -> bool:
        capability_id = row["capability_id"]
        capability_mac = row["capability_mac"]
        expires_at = row["capability_expires_at"]
        if not isinstance(capability_id, str) or not isinstance(capability_mac, str) or expires_at is None:
            return False
        expected = cls._capability_mac(
            capability_id,
            row["scope_json"],
            float(expires_at),
            record,
        )
        return hmac.compare_digest(expected, capability_mac)


_BROKER: VoiceLockdownBroker | None = None


def get_voice_lockdown_broker() -> VoiceLockdownBroker:
    global _BROKER
    if _BROKER is None:
        _BROKER = VoiceLockdownBroker()
    return _BROKER


def require_safe_baseline() -> BaselineCheck:
    check = check_safe_baseline()
    if not check.safe:
        raise LockdownDeniedError("; ".join(check.reasons))
    return check
