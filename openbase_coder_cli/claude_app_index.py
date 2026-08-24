"""Surface Openbase Claude sessions in the Claude desktop app's session list.

Claude.app keeps a private per-session index (``local_<id>.json`` files under
``~/Library/Application Support/Claude/claude-code-sessions/<ws>/<acct>/``)
and never scans the shared ``~/.claude/projects`` transcript store, so
sessions started outside the app are invisible to it. Since Openbase sessions
now live in the shared home, injecting index entries that point at their
``cliSessionId`` makes them appear (and be resumable) in the app.

The index format is the app's private schema (observed, not documented), so
this is best-effort: entries carry only observed keys, injected entries are
tracked in an Openbase ledger for idempotent updates, and every failure is
swallowed — the app owns the directory and may prune or ignore entries.
"""

from __future__ import annotations

import json
import logging
import platform
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openbase_coder_cli.paths import OPENBASE_BASE_DIR
from openbase_coder_cli.thread_sync.thread_sync_common import (
    super_agents_state_db_path,
    write_json_atomic,
)

CLAUDE_APP_SESSIONS_DIR = (
    Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
)
LEDGER_PATH = OPENBASE_BASE_DIR / "claude-app-index-ledger.json"
# Stable namespace so each Openbase session maps to one deterministic app
# index id across runs and machines.
_SESSION_ID_NAMESPACE = uuid.UUID("6e1f24d6-6f61-4c05-9e5a-9b1f6f5f7f10")

logger = logging.getLogger(__name__)


def sync_claude_app_index(
    *,
    app_sessions_dir: Path = CLAUDE_APP_SESSIONS_DIR,
    ledger_path: Path = LEDGER_PATH,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Inject/update app index entries for Openbase Claude sessions.

    Returns a summary dict; every path is best-effort and non-fatal.
    """
    if platform.system() != "Darwin":
        return {"supported": False, "reason": "not_macos"}
    target_dir = _target_index_dir(app_sessions_dir)
    if target_dir is None:
        return {"supported": False, "reason": "app_index_not_found"}

    sessions = _openbase_claude_sessions(db_path)
    ledger = _read_ledger(ledger_path)
    created = 0
    updated = 0
    for session in sessions:
        try:
            outcome = _ensure_entry(target_dir, session, ledger)
        except OSError:
            logger.warning(
                "claude_app_index event=write_failed session=%s",
                session["id"],
                exc_info=True,
            )
            continue
        if outcome == "created":
            created += 1
        elif outcome == "updated":
            updated += 1
    _write_ledger(ledger_path, ledger)
    return {
        "supported": True,
        "target_dir": str(target_dir),
        "sessions": len(sessions),
        "created": created,
        "updated": updated,
    }


def _target_index_dir(app_sessions_dir: Path) -> Path | None:
    """The app's active ``<workspace>/<account>`` index directory.

    Prefer the directory that already holds entries (most recently used when
    several do); never create one — an absent index means the app has never
    run its Claude Code surface here.
    """
    if not app_sessions_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for workspace_dir in app_sessions_dir.iterdir():
        if not workspace_dir.is_dir():
            continue
        for account_dir in workspace_dir.iterdir():
            if not account_dir.is_dir():
                continue
            entries = list(account_dir.glob("local_*.json"))
            if entries:
                latest = max(entry.stat().st_mtime for entry in entries)
                candidates.append((latest, account_dir))
    if not candidates:
        return None
    return max(candidates)[1]


def _openbase_claude_sessions(db_path: Path | None) -> list[dict[str, Any]]:
    path = db_path or super_agents_state_db_path()
    if not path.is_file():
        return []
    try:
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select id, name, cwd, model, backend_session_id,
                       created_at, updated_at
                from sessions
                where backend = 'claude_code'
                  and backend_session_id is not null
                  and cwd is not null
                """
            ).fetchall()
    except sqlite3.Error:
        logger.warning("claude_app_index event=store_unreadable", exc_info=True)
        return []
    return [dict(row) for row in rows]


def _ensure_entry(
    target_dir: Path,
    session: dict[str, Any],
    ledger: dict[str, Any],
) -> str:
    entry_id = f"local_{uuid.uuid5(_SESSION_ID_NAMESPACE, session['id'])}"
    entry_path = target_dir / f"{entry_id}.json"
    payload = {
        "sessionId": entry_id,
        "cliSessionId": session["backend_session_id"],
        "cwd": session["cwd"],
        "originCwd": session["cwd"],
        "createdAt": _epoch_ms(session.get("created_at")),
        "lastActivityAt": _epoch_ms(session.get("updated_at")),
        "model": session.get("model"),
        "isArchived": False,
        "title": session.get("name") or session["backend_session_id"],
        "titleSource": "auto",
        "permissionMode": "default",
    }
    existing: dict[str, Any] | None = None
    if entry_path.is_file():
        try:
            existing = json.loads(entry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
    if isinstance(existing, dict):
        if str(existing.get("sessionId")) != entry_id:
            # Never overwrite an entry the app owns.
            return "skipped"
        # Preserve any keys the app added; only refresh what Openbase tracks.
        merged = {**existing, **payload}
        if merged == existing:
            return "unchanged"
        write_json_atomic(entry_path, merged)
        ledger[session["id"]] = str(entry_path)
        return "updated"
    write_json_atomic(entry_path, payload)
    ledger[session["id"]] = str(entry_path)
    return "created"


def _epoch_ms(value: Any) -> int:
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return 0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1000)
    return 0


def _read_ledger(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_ledger(path: Path, ledger: dict[str, Any]) -> None:
    try:
        write_json_atomic(path, ledger)
    except OSError:
        logger.warning("claude_app_index event=ledger_write_failed", exc_info=True)
