"""Compact the Dispatcher's conversation while nobody is waiting on it.

The voice Dispatcher is one long-lived Claude Code conversation. Left alone,
Claude Code compacts it only when a request arrives with the context nearly
full, and that compaction runs on the request's critical path: on VM2
(2026-10-11) a Haiku Dispatcher (200k window) auto-compacted about once an
hour and each time stalled a live request 30-40 s, including a spoken steer
relayed 40 s late. This loop compacts it between requests instead, once its
latest call carried at least ``COMPACT_FRACTION`` of the model's window and
it has been idle for ``IDLE_SECONDS``; a request that arrives during the
compaction waits behind it (super-agents holds the session locks).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

COMPACT_FRACTION = 0.6
#: Overrides COMPACT_FRACTION (e.g. 0.01 to exercise compaction in QA without
#: first filling a 200k-token Dispatcher).
COMPACT_FRACTION_ENV = "OPENBASE_DISPATCHER_COMPACT_FRACTION"
IDLE_SECONDS = 45.0
POLL_SECONDS = 30.0
DEFAULT_CONTEXT_WINDOW = 200_000
LONG_CONTEXT_WINDOW = 1_000_000


def compact_fraction() -> float:
    try:
        value = float(os.environ.get(COMPACT_FRACTION_ENV, ""))
    except ValueError:
        return COMPACT_FRACTION
    return value if 0 < value <= 1 else COMPACT_FRACTION


def context_window(model: str | None) -> int:
    return LONG_CONTEXT_WINDOW if "[1m]" in (model or "") else DEFAULT_CONTEXT_WINDOW


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def dispatcher_idle_state(
    thread_id: str, state_db_path: Path | None = None
) -> dict | None:
    """The Dispatcher session's status, model and last activity from the store."""
    if state_db_path is None:
        from openbase_coder_cli.thread_sync.thread_sync_common import (
            super_agents_state_db_path,
        )

        state_db_path = super_agents_state_db_path()
    if not Path(state_db_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        session = conn.execute(
            "select status, active_turn_id, model, updated_at from sessions where id = ?",
            (thread_id,),
        ).fetchone()
        if session is None:
            return None
        last = conn.execute(
            "select max(coalesce(finished_at, updated_at)) as last from turns where session_id = ?",
            (thread_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return {
        "busy": bool(session["active_turn_id"])
        or session["status"] in ("running", "waiting"),
        "model": session["model"],
        "last_activity": max(
            filter(
                None,
                (
                    _parse_ts(last["last"]) if last else None,
                    _parse_ts(session["updated_at"]),
                ),
            ),
            default=None,
        ),
    }


async def compact_dispatcher_if_due(
    *, now: float | None = None, manager=None, state_db_path=None
) -> dict | None:
    from openbase_coder_cli.livekit_voice_route import get_livekit_voice_route_state

    thread_id = get_livekit_voice_route_state().dispatcher_thread_id
    if not thread_id:
        return None
    state = await asyncio.to_thread(dispatcher_idle_state, thread_id, state_db_path)
    if state is None or state["busy"]:
        return None
    now = time.time() if now is None else now
    if (
        state["last_activity"] is not None
        and now - state["last_activity"] < IDLE_SECONDS
    ):
        return None
    if manager is None:
        from openbase_coder_cli.thread_sync.session_manager import get_session_manager

        manager = get_session_manager()
    compact = getattr(manager, "compact_thread", None)
    if not callable(compact):
        return None
    min_tokens = int(compact_fraction() * context_window(state["model"]))
    result = await compact(thread_id, min_tokens)
    if result and result.get("compacted"):
        logger.info(
            "dispatcher_compaction stage=compacted thread_id=%s context_tokens=%s elapsed_ms=%s",
            thread_id,
            result.get("contextTokens"),
            result.get("elapsedMs"),
        )
    return result


async def dispatcher_compaction_loop() -> None:
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            await compact_dispatcher_if_due()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - maintenance must never take the API down
            logger.warning("Dispatcher compaction check failed", exc_info=True)
