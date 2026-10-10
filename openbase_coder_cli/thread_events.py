"""Durable, non-turn events shown in a thread's transcript.

Turns come from the coding backend's own session log; the voice layer has
no turn to leave behind when a call is transferred to a thread or returned
to the Dispatcher, so until now a Dispatcher transcript ended on
"Connected to Cooper." with no trace of the return (2026-10-10). Each thread
keeps a bounded JSON-lines file of such events; the thread detail payload
carries them as ``events`` and every client renders them as a system line.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openbase_coder_cli.cli.utils import get_data_dir

logger = logging.getLogger(__name__)

THREAD_EVENTS_DIRNAME = "thread-events"
MAX_THREAD_EVENTS = 50
VOICE_ROUTE_EVENT_KIND = "voice_route"
DISPATCHER_EVENT_LABEL = "the Dispatcher"

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _events_path(thread_id: str) -> Path:
    safe = _SAFE_ID_RE.sub("_", thread_id.strip())[:200]
    return get_data_dir() / THREAD_EVENTS_DIRNAME / f"{safe}.jsonl"


def list_thread_events(thread_id: str, *, limit: int = MAX_THREAD_EVENTS) -> list[dict]:
    """The thread's recorded events, oldest first."""
    if not thread_id:
        return []
    path = _events_path(thread_id)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        logger.debug("thread events unreadable thread_id=%s", thread_id, exc_info=True)
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("text"):
            events.append(event)
    return events[-limit:]


def record_thread_event(
    thread_id: str,
    *,
    kind: str,
    text: str,
    at: datetime | None = None,
    **fields: Any,
) -> dict:
    """Append one event to the thread, keeping the newest ``MAX_THREAD_EVENTS``."""
    event = {
        "event_id": f"evt-{uuid.uuid4().hex[:12]}",
        "kind": kind,
        "text": text,
        "at": (at or datetime.now(UTC)).isoformat(),
        **{key: value for key, value in fields.items() if value is not None},
    }
    path = _events_path(thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = list_thread_events(thread_id, limit=MAX_THREAD_EVENTS - 1)
    kept.append(event)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in kept),
        encoding="utf-8",
    )
    return event


def record_voice_route_events(
    *,
    action: str,
    dispatcher_thread_id: str | None,
    target_thread_id: str | None,
    target_label: str | None,
    at: datetime | None = None,
) -> None:
    """Leave a line in both transcripts when the call moves.

    ``transfer_to_thread`` moves the call from the Dispatcher to the target;
    ``exit_to_dispatch`` brings it back. A missing thread id skips that side.
    Never raises: a transcript note must not break the route change.
    """
    label = (target_label or "").strip() or "the agent"
    if action == "transfer_to_thread":
        dispatcher_text = f"Call transferred to {label}."
        target_text = f"Call transferred here from {DISPATCHER_EVENT_LABEL}."
    elif action == "exit_to_dispatch":
        dispatcher_text = f"Back with {DISPATCHER_EVENT_LABEL}, from {label}."
        target_text = f"Call returned to {DISPATCHER_EVENT_LABEL}."
    else:
        return
    now = at or datetime.now(UTC)
    for thread_id, text, counterpart in (
        (dispatcher_thread_id, dispatcher_text, target_thread_id),
        (target_thread_id, target_text, dispatcher_thread_id),
    ):
        if not thread_id:
            continue
        try:
            record_thread_event(
                thread_id,
                kind=VOICE_ROUTE_EVENT_KIND,
                text=text,
                at=now,
                action=action,
                counterpart_thread_id=counterpart,
                counterpart_label=label if thread_id == dispatcher_thread_id else None,
            )
        except OSError:
            logger.warning(
                "voice route event not recorded thread_id=%s action=%s",
                thread_id,
                action,
                exc_info=True,
            )
