"""HMAC-chained, redacted audit storage for voice lockdown."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from typing import Any

from .keychain import KeychainRecord


class LockdownAuditLog:
    def __init__(
        self,
        *,
        connect: Callable[[], sqlite3.Connection],
        clock: Callable[[], float],
        lock: threading.RLock,
    ) -> None:
        self._connect = connect
        self._clock = clock
        self._lock = lock

    def append(self, event_type: str, detail: dict[str, Any], *, record: KeychainRecord) -> None:
        created_at = self._clock()
        event_id = f"vla_{uuid.uuid4().hex}"
        detail_json = json.dumps(detail, separators=(",", ":"), sort_keys=True)
        audit_key = base64.b64decode(record.audit_key)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT event_mac FROM audit_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            previous_mac = previous["event_mac"] if previous else ""
            message = f"{event_id}|{created_at:.6f}|{event_type}|{detail_json}|{previous_mac}".encode()
            event_mac = hmac.new(audit_key, message, hashlib.sha256).hexdigest()
            db.execute(
                """INSERT INTO audit_events(event_id, created_at, event_type, detail_json, previous_mac, event_mac)
                VALUES(?, ?, ?, ?, ?, ?)""",
                (event_id, created_at, event_type, detail_json, previous_mac, event_mac),
            )
            db.commit()

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT sequence, event_id, created_at, event_type, detail_json FROM audit_events ORDER BY sequence DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "eventId": row["event_id"],
                "createdAt": row["created_at"],
                "eventType": row["event_type"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        ]

    def integrity_valid(self, record: KeychainRecord) -> bool:
        try:
            audit_key = base64.b64decode(record.audit_key, validate=True)
            with self._connect() as db:
                rows = db.execute("SELECT * FROM audit_events ORDER BY sequence").fetchall()
        except (ValueError, sqlite3.DatabaseError):
            return False
        if not rows:
            return False
        previous_mac = ""
        for row in rows:
            if row["previous_mac"] != previous_mac:
                return False
            message = (
                f"{row['event_id']}|{float(row['created_at']):.6f}|{row['event_type']}|"
                f"{row['detail_json']}|{previous_mac}"
            ).encode()
            expected = hmac.new(audit_key, message, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, row["event_mac"]):
                return False
            previous_mac = row["event_mac"]
        return True
