"""Durable activity inputs for the voice pool watchdog, shared across processes.

Turns already have a source of truth in Super Agents. Token issuance and job
startup have no durable timestamp there, so keep independent empty marker
files: touching one cannot overwrite another process's activity.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from openbase_coder_cli.file_lock import LOCK_EX, LOCK_UN, flock
from openbase_coder_cli.paths import OPENBASE_BASE_DIR

CALL_JOIN_GRACE_SECONDS = 300.0
_ACTIVITY_DIR = OPENBASE_BASE_DIR / "livekit-pool-activity"


@contextmanager
def call_start_lock():
    """Serialize token publication with the watchdog's check and restart.

    A final timestamp check alone leaves a check/kill race across the API and
    sync-workers processes. Job markers deliberately do not take this lock:
    restarting a worker may wait for that worker to start its jobs.
    """
    _ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    with (_ACTIVITY_DIR / "call-start.lock").open("a+b") as handle:
        flock(handle, LOCK_EX)
        try:
            yield
        finally:
            flock(handle, LOCK_UN)


def record_activity(source: Literal["token", "job"]) -> None:
    # Let write failures propagate: issuing an unprotected token is unsafe.
    _ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    if source == "token":
        with call_start_lock():
            (_ACTIVITY_DIR / source).touch()
    else:
        (_ACTIVITY_DIR / source).touch()


def activity_timestamp(source: Literal["token", "job"]) -> float:
    try:
        return (_ACTIVITY_DIR / source).stat().st_mtime
    except FileNotFoundError:
        return 0.0


def call_join_pending(now: float) -> bool:
    timestamp = activity_timestamp("token")
    return timestamp > 0 and now - timestamp < CALL_JOIN_GRACE_SECONDS


def _timestamp(value: object) -> float:
    if value is None:
        return 0.0
    if not isinstance(value, str):
        raise ValueError("Invalid turn activity timestamp")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    if not math.isfinite(timestamp):
        raise ValueError("Invalid turn activity timestamp")
    return timestamp


def _claude_activity_timestamp() -> float:
    from super_agents.agent_store import database_path

    path = database_path()
    if not path.exists():
        return 0.0
    # Do not construct Store: it initializes/migrates the database. A read-only
    # connection also prevents a missing/deleted store from being recreated.
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    ) as connection:
        rows = connection.execute(
            "SELECT MAX(updated_at) FROM turns UNION ALL "
            "SELECT MAX(updated_at) FROM sessions WHERE active_turn_id IS NOT NULL"
        ).fetchall()
    # Streaming Claude progress refreshes the active session; completion and
    # steering refresh the turn itself. Both local and Cloud Claude use this store.
    return max((_timestamp(row[0]) for row in rows), default=0.0)


def latest_activity_timestamp() -> float:
    """Latest token, job, or dispatcher/thread turn, including completed turns.

    Read the existing state directly so corrupt data raises rather than being
    silently treated as an idle workspace. The watchdog defers on read errors.
    """
    from super_agents.app_server_client import DEFAULT_STATE_FILE

    latest = max(
        activity_timestamp("token"),
        activity_timestamp("job"),
        _claude_activity_timestamp(),
    )
    path = Path(
        os.environ.get("SUPER_AGENTS_STATE_FILE") or DEFAULT_STATE_FILE
    ).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return latest
    for session in payload.get("sessions", {}).values():
        # updatedAt is also refreshed by metadata; lastEventAt and the turn
        # lifecycle fields count actual work even after that work completes.
        for key in ("lastEventAt", "lastStartedAt", "lastFinishedAt"):
            latest = max(latest, _timestamp(session.get(key)))
        for turn in (session.get("turns") or {}).values():
            latest = max(latest, _timestamp(turn.get("updatedAt")))
    return latest
