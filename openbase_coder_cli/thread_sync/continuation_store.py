"""Openbase-owned continuation relationships and immutable context snapshots."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

from openbase_coder_cli.cli.utils import get_data_dir


@contextmanager
def connection():
    path = get_data_dir() / "thread-continuations.sqlite3"
    db = sqlite3.connect(path, timeout=5)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS continuations (
            operation_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
            backend TEXT NOT NULL, state TEXT NOT NULL, destination_id TEXT,
            data TEXT NOT NULL DEFAULT '{}'
        )""")
        db.execute(
            "CREATE INDEX IF NOT EXISTS continuation_source ON continuations(source_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS continuation_destination ON continuations(destination_id)"
        )
        with db:
            yield db
    finally:
        db.close()


def _record(row):
    return {**dict(row), **json.loads(row["data"])} if row else None


def reserve(operation_id: str, source_id: str, backend: str) -> tuple[dict, bool]:
    with connection() as db:
        created = db.execute(
            "INSERT OR IGNORE INTO continuations(operation_id, source_id, backend, state) VALUES (?, ?, ?, 'preparing')",
            (operation_id, source_id, backend),
        ).rowcount
        record = _record(
            db.execute(
                "SELECT * FROM continuations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        )
    if record["source_id"] != source_id or record["backend"] != backend:
        raise ValueError(
            "This request ID was already used for a different continuation."
        )
    return record, bool(created)


def update(
    operation_id: str, *, state: str, destination_id: str | None = None, **data
) -> dict:
    with connection() as db:
        row = db.execute(
            "SELECT * FROM continuations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        payload = {**json.loads(row["data"]), **data}
        db.execute(
            "UPDATE continuations SET state = ?, destination_id = COALESCE(?, destination_id), data = ? WHERE operation_id = ?",
            (state, destination_id, json.dumps(payload), operation_id),
        )
        return _record(
            db.execute(
                "SELECT * FROM continuations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        )


def for_destination(thread_id: str) -> dict | None:
    if not (get_data_dir() / "thread-continuations.sqlite3").exists():
        return None
    with connection() as db:
        return _record(
            db.execute(
                "SELECT * FROM continuations WHERE destination_id = ?", (thread_id,)
            ).fetchone()
        )


def for_operation(operation_id: str) -> dict | None:
    with connection() as db:
        return _record(
            db.execute(
                "SELECT * FROM continuations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        )


def links(thread_id: str) -> dict:
    if not (get_data_dir() / "thread-continuations.sqlite3").exists():
        return {}
    with connection() as db:
        rows = db.execute(
            """SELECT source_id, destination_id,
                json_extract(data, '$.name') AS name,
                json_extract(data, '$.source_name') AS source_name,
                json_extract(data, '$.omitted') AS omitted,
                json_array_length(data, '$.messages') AS message_count
            FROM continuations WHERE (source_id = ? OR destination_id = ?) AND state = 'ready'""",
            (thread_id, thread_id),
        ).fetchall()
    result = {"continuations": []}
    for row in rows:
        record = dict(row)
        if record["destination_id"] == thread_id:
            result["continued_from"] = {
                "thread_id": record["source_id"],
                "name": record["source_name"],
            }
            result["continuation_context"] = {
                "omitted": bool(record["omitted"]),
                "message_count": record["message_count"],
            }
        else:
            result["continuations"].append(
                {"thread_id": record["destination_id"], "name": record["name"]}
            )
    return result
