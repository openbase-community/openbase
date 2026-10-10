"""Durable, atomic announcement receipts, separate from agent conversation state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


class AnnouncementLedger:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def transaction(self, thread_id: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS announcements "
                "(thread_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM announcements WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            state = json.loads(row[0]) if row else {"intro": {}, "turn": {}}
            yield state
            connection.execute(
                "INSERT INTO announcements VALUES (?, ?) "
                "ON CONFLICT(thread_id) DO UPDATE SET state=excluded.state",
                (thread_id, json.dumps(state)),
            )

    def edit(self, thread_id, operation):
        with self.transaction(thread_id) as state:
            return operation(state)

    def mark_delegated(self, turn_id: str, parent_id: str):
        # Turn IDs are backend-generated identities, not user-selected names.
        self.edit("origin:" + turn_id, lambda state: state.update(parent_id=parent_id))

    def is_delegated(self, turn_id: str) -> bool:
        return self.edit(
            "origin:" + turn_id, lambda state: bool(state.get("parent_id"))
        )

    def read(self, key):
        if not self.path.exists():
            return None
        with closing(
            sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        ) as connection:
            row = connection.execute(
                "SELECT state FROM announcements WHERE thread_id = ?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else None


def turn_revision(turn):
    prompts = (turn.prompt, *(str(s.get("text", "")) for s in turn.steers))
    return hashlib.sha256(json.dumps(prompts).encode()).hexdigest()
