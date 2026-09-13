"""Local origin metadata for threads (manual UI entry vs agent-spawned).

Only the console/desktop/iOS "new thread" endpoint stamps ``manual``.
Threads with no recorded origin (Super Agents MCP, dispatcher, voice) are
treated as agent-started, so notification producers default-deny them.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openbase_coder_cli.cli.utils import get_data_dir

ORIGINS_FILE = "thread-origins.json"
MANUAL_ORIGIN = "manual"
MAX_ORIGIN_ENTRIES = 2000

_lock = threading.Lock()


def set_thread_origin(thread_id: str, origin: str) -> None:
    """Record how a thread was started."""
    normalized = _normalize_thread_id(thread_id)
    if not normalized:
        raise ValueError("thread_id is required")

    with _lock:
        origins = _read_origins_unlocked()
        origins[normalized] = {
            "origin": origin,
            "created_at": _utc_now(),
        }
        if len(origins) > MAX_ORIGIN_ENTRIES:
            oldest_first = sorted(
                origins.items(),
                key=lambda item: item[1].get("created_at") or "",
            )
            for stale_id, _entry in oldest_first[: len(origins) - MAX_ORIGIN_ENTRIES]:
                origins.pop(stale_id, None)
        _write_origins_unlocked(origins)


def thread_origin(thread_id: str | None) -> str | None:
    normalized = _normalize_thread_id(thread_id)
    if not normalized:
        return None
    with _lock:
        entry = _read_origins_unlocked().get(normalized)
    origin = entry.get("origin") if isinstance(entry, dict) else None
    return origin if isinstance(origin, str) else None


def is_manual_thread(thread_id: str | None) -> bool:
    """True only for threads explicitly stamped as manual UI entries."""
    return thread_origin(thread_id) == MANUAL_ORIGIN


def _origins_path() -> Path:
    return get_data_dir() / ORIGINS_FILE


def _read_origins_unlocked() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(_origins_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    threads = payload.get("threads") if isinstance(payload, dict) else None
    if not isinstance(threads, dict):
        return {}
    origins: dict[str, dict[str, Any]] = {}
    for raw_thread_id, raw_entry in threads.items():
        thread_id = _normalize_thread_id(raw_thread_id)
        if not thread_id or not isinstance(raw_entry, dict):
            continue
        origins[thread_id] = raw_entry
    return origins


def _write_origins_unlocked(origins: dict[str, dict[str, Any]]) -> None:
    path = _origins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as tmp:
        json.dump({"threads": origins}, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _normalize_thread_id(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
