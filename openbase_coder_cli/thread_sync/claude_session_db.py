"""Super Agents SQLite integration for synced Claude sessions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .claude_jsonl import _iso_from_ms, _iso_now, _preview
from .claude_models import DEFAULT_LEGACY_SUPER_AGENTS_STATE_PATH, ClaudeSessionSnapshot
from .thread_import import _string
from .thread_sync_common import super_agents_state_db_path, translate_home_path


def _translated_metadata_cwd(
    metadata: dict[str, Any], target_user_home: Path
) -> str | None:
    source_value = _string(metadata.get("source_user_home"))
    source_home = Path(source_value) if source_value else None
    if source_home is not None and not source_home.is_absolute():
        source_home = None
    return translate_home_path(
        _string(metadata.get("cwd")),
        source_home=source_home,
        target_home=target_user_home,
    )


def _translate_super_agent_session_cwds(
    db_path: Path | None, target_user_home: Path
) -> None:
    """Migrate foreign-home cwd values retained by older thread imports."""
    resolved_db = db_path or _super_agents_db_path()
    if not resolved_db.is_file():
        return
    with sqlite3.connect(resolved_db) as conn:
        conn.row_factory = sqlite3.Row
        has_sessions = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        ).fetchone()
        if has_sessions is None:
            return
        rows = conn.execute("SELECT id, cwd FROM sessions").fetchall()
        for row in rows:
            translated = translate_home_path(row["cwd"], target_home=target_user_home)
            if translated and translated != row["cwd"]:
                conn.execute(
                    "UPDATE sessions SET cwd = ? WHERE id = ?",
                    (translated, row["id"]),
                )
        conn.commit()


def _backfill_openbase_session_metadata(
    snapshot: ClaudeSessionSnapshot,
    *,
    db_path: Path | None,
    cwd_override: str | None = None,
) -> None:
    resolved_db = db_path or _super_agents_db_path()
    resolved_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(resolved_db) as conn:
        _ensure_super_agents_schema(conn)
        existing = conn.execute(
            "select * from sessions where backend_session_id = ?",
            (snapshot.session_id,),
        ).fetchone()
        if existing is None:
            session_id = f"claude_{snapshot.session_id.replace('-', '')}"
            name = _unique_session_name(conn, snapshot.name)
            created_at = _iso_from_ms(snapshot.created_at_ms) or _iso_now()
            updated_at = _iso_from_ms(snapshot.updated_at_ms) or created_at
            conn.execute(
                """
                insert into sessions (
                    id, name, cwd, command_json, status, last_observed_state,
                    last_useful_message, backend_session_id, log_path,
                    raw_log_path, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    name,
                    cwd_override or snapshot.cwd or str(Path.home()),
                    json.dumps(["claude", "--resume", snapshot.session_id]),
                    # Idle synced sessions are "completed"; "waiting" means
                    # blocked on user input and counts as active.
                    "completed",
                    _session_observed_state(snapshot),
                    snapshot.latest_assistant_message,
                    snapshot.session_id,
                    None,
                    None,
                    created_at,
                    updated_at,
                ),
            )
            return
        updates: dict[str, Any] = {}
        if (
            snapshot.latest_assistant_message
            and existing["last_useful_message"] != snapshot.latest_assistant_message
        ):
            updates["last_useful_message"] = snapshot.latest_assistant_message
        if (
            _string(existing["name"])
            and _string(existing["name"]).startswith("<")
            and snapshot.name
            and not snapshot.name.startswith("<")
        ):
            # Self-heal names imported before harness markup was stripped.
            updates["name"] = _unique_session_name(conn, snapshot.name)
        desired_cwd = cwd_override or snapshot.cwd
        if desired_cwd and existing["cwd"] != desired_cwd:
            updates["cwd"] = desired_cwd
        if _should_refresh_observed_state(existing["last_observed_state"]):
            updates["last_observed_state"] = _session_observed_state(snapshot)
        if not updates:
            return
        updates["updated_at"] = _iso_from_ms(snapshot.updated_at_ms) or _iso_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"update sessions set {assignments} where id = ?",
            [*updates.values(), existing["id"]],
        )


def _ensure_super_agents_schema(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        create table if not exists sessions (
            id text primary key,
            name text not null unique,
            agent_name text,
            developer_instructions text,
            cwd text not null,
            command_json text not null,
            model text,
            status text not null,
            pid integer,
            active_turn_id text,
            last_turn_id text,
            last_observed_state text,
            last_useful_message text,
            backend_session_id text,
            last_exit_code integer,
            log_path text,
            raw_log_path text,
            created_at text not null,
            updated_at text not null
        );
        create table if not exists turns (
            id text primary key,
            session_id text not null references sessions(id) on delete cascade,
            prompt text not null,
            mode text,
            model text,
            reasoning_effort text,
            status text not null,
            attempts integer not null default 0,
            last_error text,
            last_useful_message text,
            created_at text not null,
            updated_at text not null,
            finished_at text
        );
        create index if not exists turns_session_idx on turns(session_id, created_at);
        """
    )
    session_columns = {
        row["name"] for row in conn.execute("pragma table_info(sessions)").fetchall()
    }
    session_defaults = {
        "agent_name": "text",
        "developer_instructions": "text",
        "model": "text",
        "pid": "integer",
        "active_turn_id": "text",
        "last_turn_id": "text",
        "last_observed_state": "text",
        "last_useful_message": "text",
        "backend_session_id": "text",
        "last_exit_code": "integer",
        "log_path": "text",
        "raw_log_path": "text",
    }
    for column, column_type in session_defaults.items():
        if column not in session_columns:
            conn.execute(f"alter table sessions add column {column} {column_type}")
    turn_columns = {
        row["name"] for row in conn.execute("pragma table_info(turns)").fetchall()
    }
    turn_defaults = {
        "mode": "text",
        "model": "text",
        "reasoning_effort": "text",
        "attempts": "integer not null default 0",
        "last_error": "text",
        "last_useful_message": "text",
        "finished_at": "text",
    }
    for column, column_type in turn_defaults.items():
        if column not in turn_columns:
            conn.execute(f"alter table turns add column {column} {column_type}")


def _active_claude_session_ids(db_path: Path | None = None) -> set[str]:
    active: set[str] = set()
    resolved_db = db_path or _super_agents_db_path()
    if resolved_db.exists():
        with sqlite3.connect(resolved_db) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    select backend_session_id from sessions
                    where backend_session_id is not null
                    and (status = 'running' or active_turn_id is not null)
                    """
                ).fetchall()
            except sqlite3.Error:
                rows = []
        active.update(
            row["backend_session_id"] for row in rows if row["backend_session_id"]
        )
    active.update(_active_claude_session_ids_from_legacy_state())
    return active


def _active_claude_session_ids_from_legacy_state(
    state_path: Path = DEFAULT_LEGACY_SUPER_AGENTS_STATE_PATH,
) -> set[str]:
    if not state_path.exists():
        return set()
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    if not isinstance(sessions, dict):
        return set()
    active: set[str] = set()
    for value in sessions.values():
        if not isinstance(value, dict):
            continue
        status = value.get("status") or value.get("lastStatus")
        active_turn = value.get("activeTurnId") or value.get("active_turn_id")
        backend_session_id = _string(
            value.get("backendSessionId") or value.get("backend_session_id")
        )
        if backend_session_id and (status == "running" or active_turn):
            active.add(backend_session_id)
    return active


def _super_agents_db_path() -> Path:
    return super_agents_state_db_path()


def _unique_session_name(conn: sqlite3.Connection, base_name: str) -> str:
    name = _preview(base_name, limit=80) or "Claude Code session"
    candidate = name
    suffix = 2
    while conn.execute(
        "select 1 from sessions where name = ?", (candidate,)
    ).fetchone():
        suffix_text = f" ({suffix})"
        candidate = f"{name[: 80 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return candidate


def _session_observed_state(snapshot: ClaudeSessionSnapshot) -> str:
    payload = {
        "source": "claude_thread_sync",
        "backend": "claude_code",
        "backend_session_id": snapshot.session_id,
        "project_key": snapshot.project_key,
        "root_relative_path": snapshot.relative_root.as_posix(),
        "cwd": snapshot.cwd,
        "root_sha256": snapshot.fingerprint.get("root_sha256"),
        "tree_sha256": snapshot.fingerprint.get("tree_sha256"),
        "created_at": _iso_from_ms(snapshot.created_at_ms),
        "updated_at": _iso_from_ms(snapshot.updated_at_ms),
        "observed_at": _iso_now(),
    }
    return json.dumps(payload, sort_keys=True)


def _should_refresh_observed_state(value: str | None) -> bool:
    if not value:
        return True
    if value == "Claude Code session imported by thread sync":
        return True
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(raw, dict) and raw.get("source") == "claude_thread_sync"
