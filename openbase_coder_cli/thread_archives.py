"""Local archive visibility for backends without a native archive operation.

Thread contents remain in the backend. The independent, versioned SQLite
store survives manager restarts and serializes writes from API processes.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from openbase_coder_cli.cli.utils import get_data_dir

SCHEMA_VERSION = 1
ARCHIVES_FILE = "thread-archives.sqlite3"


def _check_schema(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT schema_version FROM archive_metadata WHERE id = 1"
    ).fetchone()
    if row is None or row[0] != SCHEMA_VERSION:
        raise RuntimeError("Unsupported thread archive schema; update the CLI")


def archived_thread_ids(backend: str) -> set[str]:
    """Read this execution backend's archived IDs without creating a store."""
    path = get_data_dir() / ARCHIVES_FILE
    if not path.exists():
        return set()
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as connection:
        _check_schema(connection)
        return {
            row[0]
            for row in connection.execute(
                "SELECT thread_id FROM archived_threads WHERE backend = ?", (backend,)
            )
        }


def archive_backend_thread(backend: str, thread_id: str) -> None:
    """Durably hide one exact backend identity without touching its history."""
    if not backend or not thread_id:
        raise ValueError("backend and thread_id are required")
    path = get_data_dir() / ARCHIVES_FILE
    with closing(sqlite3.connect(path, timeout=10)) as connection, connection:
        # Publish the schema and first marker in one transaction. Readers must
        # never observe a metadata table without its schema-version row.
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS archive_metadata "
            "(id INTEGER PRIMARY KEY CHECK(id = 1), schema_version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO archive_metadata (id, schema_version) VALUES (1, ?)",
            (SCHEMA_VERSION,),
        )
        _check_schema(connection)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS archived_threads "
            "(backend TEXT NOT NULL, thread_id TEXT NOT NULL, "
            "archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (backend, thread_id))"
        )
        connection.execute(
            "INSERT OR IGNORE INTO archived_threads (backend, thread_id) VALUES (?, ?)",
            (backend, thread_id),
        )
