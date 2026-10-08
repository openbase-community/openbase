"""Where a pushed thread lives now: the local "moved" markers and arrivals.

A thread pushed to another computer stays visible here as a read-only copy
(device thread sync keeps mirroring it) but must not run turns in two
places. This module is the small persistent record behind that rule:

- **moves** (keyed by the local thread id): the push state of a thread that
  is leaving or has left this computer. ``pushing`` and ``uncertain`` block
  new turns while the transfer is in flight or its outcome is unknown;
  ``moved`` blocks them for good and names the destination; ``failed``
  records a push that stopped safely (the thread stays usable here).
- **arrivals** (keyed by push operation id, on the receiving computer): the
  outcome of an accepted push, so a retried request returns the same result
  instead of importing or starting the follow-up turn twice.

Both live in one JSON file under the data dir. Reads on hot paths (the
thread list annotates every row) go through a stat-keyed cache; writes take
an in-process lock plus an advisory file lock and replace the file
atomically.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openbase_coder_cli.json_snapshot import JsonFileSnapshot

MOVES_FILE = "thread-moves.json"
STATE_PUSHING = "pushing"
STATE_UNCERTAIN = "uncertain"
STATE_MOVED = "moved"
STATE_FAILED = "failed"
# States in which this computer must not start, queue or steer turns.
BLOCKING_STATES = frozenset({STATE_PUSHING, STATE_UNCERTAIN, STATE_MOVED})
MAX_ARRIVALS = 500

_lock = threading.RLock()


class ThreadMovedError(ValueError):
    """A turn was requested on a thread that left (or is leaving) this computer."""

    def __init__(self, message: str, move: dict[str, Any]):
        super().__init__(message)
        self.move = move


def _parse(payload: Any) -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = {"moves": {}, "arrivals": {}}
    if not isinstance(payload, dict):
        return data
    for key in data:
        section = payload.get(key)
        if isinstance(section, dict):
            data[key] = {
                str(item_key): value
                for item_key, value in section.items()
                if isinstance(value, dict)
            }
    return data


_snapshot: JsonFileSnapshot[dict[str, dict[str, Any]]] = JsonFileSnapshot(_parse)


def store_path() -> Path:
    from openbase_coder_cli.cli.utils import get_data_dir

    return get_data_dir() / MOVES_FILE


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@contextlib.contextmanager
def _locked_store():
    """Yield a private copy of the store; it is written back on clean exit."""
    from openbase_coder_cli.file_lock import LOCK_EX, LOCK_UN, flock

    path = store_path()
    with _lock:
        lock_path = path.with_name(path.name + ".lock")
        with lock_path.open("a+b") as handle:
            flock(handle, LOCK_EX)
            try:
                data = _snapshot.read_fresh(path)
                yield data
                _write(path, data)
            finally:
                flock(handle, LOCK_UN)
        _snapshot.invalidate(path)


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as tmp:
        json.dump(data, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_name = tmp.name
    os.replace(tmp_name, path)


# --- moves (source side) ---------------------------------------------------


def get_move(thread_id: str | None) -> dict[str, Any] | None:
    """The push record of a local thread (a read-only shared value)."""
    if not thread_id:
        return None
    return _snapshot.get(store_path())["moves"].get(str(thread_id))


def set_move(thread_id: str, **fields: Any) -> dict[str, Any]:
    """Create or update a thread's push record; returns a copy of it."""
    with _locked_store() as data:
        record = dict(data["moves"].get(thread_id) or {})
        record.update(fields)
        record["thread_id"] = thread_id
        record["updated_at"] = now_iso()
        data["moves"][thread_id] = record
        return copy.deepcopy(record)


def clear_move(thread_id: str) -> dict[str, Any] | None:
    """Forget a thread's push record (it is usable here again)."""
    with _locked_store() as data:
        return data["moves"].pop(thread_id, None)


def moved_to_payload(thread_id: str | None) -> dict[str, Any] | None:
    """The ``moved_to`` field of a thread payload (None when not moving)."""
    move = get_move(thread_id)
    if not move or move.get("state") not in BLOCKING_STATES:
        return None
    target = move.get("target") or {}
    return {
        "state": move.get("state"),
        "device": target.get("name"),
        "host": target.get("host"),
        "thread_id": move.get("target_thread_id") or move.get("thread_id"),
        "at": move.get("moved_at") or move.get("started_at"),
    }


def blocking_move_message(move: dict[str, Any]) -> str:
    device = (move.get("target") or {}).get("name") or "another computer"
    if move.get("state") == STATE_MOVED:
        return f"This thread moved to {device}. Continue it there."
    if move.get("state") == STATE_UNCERTAIN:
        return (
            f"The push of this thread to {device} did not finish. "
            "Retry the push to complete it."
        )
    return f"This thread is being pushed to {device}."


def ensure_thread_writable(thread_id: str | None) -> None:
    """Raise ``ThreadMovedError`` when this thread must not run turns here."""
    move = get_move(thread_id)
    if move and move.get("state") in BLOCKING_STATES:
        raise ThreadMovedError(blocking_move_message(move), dict(move))


# --- arrivals (target side) ------------------------------------------------


def get_arrival(operation_id: str) -> dict[str, Any] | None:
    return _snapshot.get(store_path())["arrivals"].get(str(operation_id))


def set_arrival(operation_id: str, **fields: Any) -> dict[str, Any]:
    with _locked_store() as data:
        arrivals = data["arrivals"]
        record = dict(arrivals.get(operation_id) or {"created_at": now_iso()})
        record.update(fields)
        record["operation_id"] = operation_id
        record["updated_at"] = now_iso()
        arrivals[operation_id] = record
        if len(arrivals) > MAX_ARRIVALS:
            oldest = sorted(
                arrivals.items(), key=lambda item: item[1].get("created_at") or ""
            )
            for stale_id, _ in oldest[: len(arrivals) - MAX_ARRIVALS]:
                arrivals.pop(stale_id, None)
        return copy.deepcopy(record)
