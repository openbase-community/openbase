"""Shared helpers for the Codex state DB, rollouts, and session index.

Used by the cross-device thread exchange and the Claude session sync; the
single shared ``~/.codex`` home is the only local thread store.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import subprocess
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openbase_coder_cli.paths import (
    CODEX_HOME_DIR,  # noqa: F401  (re-export convenience)
)

from .thread_sync_common import (  # noqa: F401
    path_stable,
    super_agents_state_db_path,
)

STATE_DB_NAME = "state_5.sqlite"
_STATE_DB_PATTERN = re.compile(r"state_(\d+)\.sqlite")
SESSION_INDEX_NAME = "session_index.jsonl"
TERMINAL_EVENT_TYPES = {"task_complete", "turn_aborted"}
DEFAULT_SUPER_AGENTS_STATE_PATH = Path.home() / ".super-agents" / "state.json"
DEFAULT_SYNC_MAX_AGE_DAYS = 15
FINGERPRINT_MATCH_KEYS = (
    "rollout_sha256",
    "rollout_size",
    "updated_at_ms",
    "updated_at",
)

logger = logging.getLogger(__name__)



def state_db_path(home: Path) -> Path:
    """Path of the newest-schema ``state_<N>.sqlite`` in ``home``.

    Codex leaves superseded state databases in place when a schema migration
    creates the next ``state_<N>`` file, so a pinned filename would keep
    syncing the frozen pre-migration database after a Codex upgrade.
    """
    best_version: int | None = None
    best_path = home / STATE_DB_NAME
    for candidate in home.glob("state_*.sqlite"):
        match = _STATE_DB_PATTERN.fullmatch(candidate.name)
        if match is None:
            continue
        version = int(match.group(1))
        if best_version is None or version > best_version:
            best_version = version
            best_path = candidate
    return best_path

def _state_db_version(path: Path) -> int | None:
    match = _STATE_DB_PATTERN.fullmatch(path.name)
    return int(match.group(1)) if match else None

class ThreadTransferError(RuntimeError):
    """Raised when a thread cannot be transferred conservatively."""

@dataclass(frozen=True)
class ThreadSyncSafety:
    safe: bool
    reason: str

def _row_updated_ms(row: dict[str, Any]) -> int:
    updated_at_ms = row.get("updated_at_ms")
    if isinstance(updated_at_ms, int):
        return updated_at_ms
    updated_at = row.get("updated_at")
    if isinstance(updated_at, int):
        return updated_at * 1000
    return 0

def _thread_safe_for_sync(
    row: dict[str, Any],
    home: Path,
    thread_id: str,
    *,
    stability_delay_seconds: float,
) -> ThreadSyncSafety:
    rollout = _source_rollout_path(row, home, thread_id)
    if rollout is None:
        return ThreadSyncSafety(False, "rollout_not_found")
    if not path_stable(rollout, stability_delay_seconds):
        return ThreadSyncSafety(False, "skipped_unstable")
    terminal, has_undecodable_lines = _rollout_terminal_event(rollout)
    if has_undecodable_lines and _rollout_open_for_write(rollout):
        return ThreadSyncSafety(False, "skipped_active")
    if terminal is None:
        return ThreadSyncSafety(False, "rollout_malformed")
    if terminal not in TERMINAL_EVENT_TYPES:
        if _rollout_open_for_write(rollout):
            return ThreadSyncSafety(False, "skipped_active")
        return ThreadSyncSafety(False, "non_terminal")
    return ThreadSyncSafety(True, "safe")

def _target_row_safe_for_overwrite(
    row: dict[str, Any],
    home: Path,
    thread_id: str,
) -> bool:
    rollout = _source_rollout_path(row, home, thread_id)
    if rollout is None or _rollout_open_for_write(rollout):
        return False
    terminal, _ = _rollout_terminal_event(rollout)
    return terminal in TERMINAL_EVENT_TYPES

def _rollout_terminal_event(path: Path) -> tuple[str | None, bool]:
    """Last parseable event type, plus whether any line failed to decode.

    Undecodable lines are skipped rather than poisoning the rollout: a crash
    mid-append leaves a truncated tail forever, and treating that as malformed
    would permanently strand the thread from sync. Callers must still treat
    undecodable content in a file that is open for write as an in-progress
    append, not history.
    """
    last_event_type: str | None = None
    has_undecodable_lines = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            has_undecodable_lines = True
            continue
        if not isinstance(event, dict):
            has_undecodable_lines = True
            continue
        event_type = _string(event.get("type"))
        payload = event.get("payload")
        if event_type == "event_msg" and isinstance(payload, dict):
            last_event_type = _string(payload.get("type"))
        elif event_type:
            last_event_type = event_type
    return last_event_type, has_undecodable_lines

def _rollout_open_for_write(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["lsof", "-nP", "--", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        fd = parts[3]
        if "w" in fd or "u" in fd:
            return True
    return False

def _rollout_has_prefix(candidate: Path, prefix: Path) -> bool:
    try:
        candidate_size = candidate.stat().st_size
        prefix_size = prefix.stat().st_size
    except OSError:
        return False
    if prefix_size > candidate_size:
        return False

    with candidate.open("rb") as candidate_handle, prefix.open("rb") as prefix_handle:
        for prefix_chunk in iter(lambda: prefix_handle.read(1024 * 1024), b""):
            if candidate_handle.read(len(prefix_chunk)) != prefix_chunk:
                return False
    return True

def _thread_fingerprint(
    row: dict[str, Any],
    home: Path,
    thread_id: str,
) -> dict[str, Any] | None:
    rollout = _source_rollout_path(row, home, thread_id)
    return _fingerprint_from_rollout_path(rollout, row)

def _fingerprint_from_rollout_path(
    rollout: Path | None,
    row: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if rollout is None or not rollout.exists():
        return None
    digest = hashlib.sha256()
    with rollout.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = rollout.stat()
    return {
        "rollout_sha256": digest.hexdigest(),
        "rollout_size": stat.st_size,
        "updated_at_ms": row.get("updated_at_ms") if row else None,
        "updated_at": row.get("updated_at") if row else None,
    }

def _active_super_agent_thread_ids(
    state_path: Path = DEFAULT_SUPER_AGENTS_STATE_PATH,
    *,
    db_path: Path | None = None,
) -> set[str]:
    active = _active_super_agent_thread_ids_from_db(db_path)
    if not state_path.exists():
        return active
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return active
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    if not isinstance(sessions, dict):
        return active
    for key, value in sessions.items():
        if not isinstance(value, dict):
            continue
        status = value.get("lastStatus")
        active_turn = value.get("activeTurnId")
        thread_id = _string(value.get("threadId")) or (
            key if isinstance(key, str) else None
        )
        if thread_id and status in {"running", "waiting"} and active_turn:
            active.add(thread_id)
    return active

def _active_super_agent_thread_ids_from_db(db_path: Path | None = None) -> set[str]:
    """Read active Super Agents thread ids from the SQLite agent store."""
    resolved_db = db_path or super_agents_state_db_path()
    if not resolved_db.exists():
        return set()
    with closing(sqlite3.connect(resolved_db)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                select id, backend_session_id from sessions
                where status = 'running' or active_turn_id is not null
                """
            ).fetchall()
        except sqlite3.Error:
            rows = []
    active: set[str] = set()
    for row in rows:
        thread_id = _string(row["backend_session_id"]) or _thread_id_from_store_id(
            _string(row["id"])
        )
        if thread_id:
            active.add(thread_id)
    return active

def _thread_id_from_store_id(store_id: str | None) -> str | None:
    """Recover a Codex thread UUID from a prefixed agent-store session id."""
    if not store_id or not store_id.startswith("codex_"):
        return None
    try:
        return str(uuid.UUID(hex=store_id.removeprefix("codex_")))
    except ValueError:
        return None

def _thread_rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    with _managed_connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM threads WHERE archived = 0 ORDER BY updated_at_ms DESC, updated_at DESC"
            )
        ]

def _thread_row(db_path: Path, thread_id: str) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    with _managed_connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
    return dict(row) if row is not None else None

def _copy_thread_state_row(
    source_db: Path,
    target_db: Path,
    table: str,
    thread_id: str,
    *,
    overrides: dict[str, Any] | None = None,
    overwrite: bool,
) -> None:
    with (
        _managed_connect(source_db) as source_conn,
        _managed_connect(target_db) as target_conn,
    ):
        source_conn.row_factory = sqlite3.Row
        target_conn.row_factory = sqlite3.Row
        source_columns = _table_columns(source_conn, table)
        target_columns = _table_columns(target_conn, table)
        columns = [column for column in source_columns if column in target_columns]
        row = source_conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            return
        values = dict(row)
        values.update(overrides or {})
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(columns)
        verb = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
        target_conn.execute(
            f"{verb} INTO {table} ({column_sql}) VALUES ({placeholders})",
            [values.get(column) for column in columns],
        )

def _copy_thread_dynamic_tools(
    source_db: Path,
    target_db: Path,
    thread_id: str,
    *,
    overwrite: bool,
) -> None:
    with (
        _managed_connect(source_db) as source_conn,
        _managed_connect(target_db) as target_conn,
    ):
        source_conn.row_factory = sqlite3.Row
        target_conn.row_factory = sqlite3.Row
        if not _has_table(source_conn, "thread_dynamic_tools") or not _has_table(
            target_conn,
            "thread_dynamic_tools",
        ):
            return
        columns = [
            column
            for column in _table_columns(source_conn, "thread_dynamic_tools")
            if column in _table_columns(target_conn, "thread_dynamic_tools")
        ]
        rows = source_conn.execute(
            f"SELECT {', '.join(columns)} FROM thread_dynamic_tools WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
        if overwrite:
            target_conn.execute(
                "DELETE FROM thread_dynamic_tools WHERE thread_id = ?",
                (thread_id,),
            )
        verb = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(columns)
        for row in rows:
            values = dict(row)
            target_conn.execute(
                f"{verb} INTO thread_dynamic_tools ({column_sql}) VALUES ({placeholders})",
                [values.get(column) for column in columns],
            )

def _source_rollout_path(
    row: dict[str, Any],
    normal_home: Path,
    thread_id: str,
) -> Path | None:
    rollout_path = _string(row.get("rollout_path"))
    if rollout_path:
        path = Path(rollout_path).expanduser()
        if path.exists():
            return path
    matches = sorted((normal_home / "sessions").glob(f"**/*{thread_id}.jsonl"))
    return matches[-1] if matches else None

def _target_rollout_path(
    source_rollout: Path, normal_home: Path, voice_home: Path
) -> Path:
    try:
        relative = source_rollout.relative_to(normal_home)
    except ValueError:
        relative = Path("sessions") / source_rollout.name
    return voice_home / relative

def _latest_session_index_entries(index_path: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    if not index_path.exists():
        return entries
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        thread_id = _string(entry.get("id"))
        if thread_id:
            entries[thread_id] = entry
    return entries

def _append_session_index_entry(
    thread_id: str,
    *,
    source_index: Path,
    target_index: Path,
    fallback_title: str,
    fallback_updated_at: str | None,
    overwrite: bool,
) -> None:
    target_entries = _latest_session_index_entries(target_index)
    if thread_id in target_entries and not overwrite:
        return
    source_entry = _latest_session_index_entries(source_index).get(thread_id)
    entry = (
        dict(source_entry)
        if source_entry
        else {
            "id": thread_id,
            "thread_name": fallback_title,
            "updated_at": fallback_updated_at,
        }
    )
    if fallback_updated_at:
        entry["updated_at"] = fallback_updated_at
    target_index.parent.mkdir(parents=True, exist_ok=True)
    with target_index.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, separators=(",", ":")) + "\n")

def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)

@contextmanager
def _managed_connect(path: Path):
    conn = _connect(path)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()

def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]

def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None

def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None

def _timestamp_to_iso(value: Any) -> str | None:
    if not isinstance(value, int):
        return None
    if value > 10_000_000_000:
        seconds = value / 1000
    else:
        seconds = value
    from datetime import UTC, datetime

    return datetime.fromtimestamp(seconds, tz=UTC).isoformat().replace("+00:00", "Z")
