"""Fallback store for undeliverable ``openbase-coder user say`` announcements.

When an agent's voice announcement cannot be delivered (for example there is no
active LiveKit voice room to target, or the LiveKit publish errors out) the
spoken message would otherwise be silently lost. We persist it here instead so
the user still finds out an agent tried to reach them: the iOS app polls the
``/api/missed-announcements/`` endpoint and raises a local notification for each
new entry (the same local-notification + polling mechanism used for approval
requests and new reports).

The store is a small JSON file capped at ``MAX_MISSED_ANNOUNCEMENTS`` entries,
newest first. It is machine-local (like other ``~/.openbase`` runtime state) and
must never sync between devices.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openbase_coder_cli.paths import MISSED_ANNOUNCEMENTS_JSON_PATH

ANNOUNCEMENTS_KEY = "announcements"
MAX_MISSED_ANNOUNCEMENTS = 50
# Bounded so a runaway loop of failed says can't grow the file without limit.
MAX_TEXT_LENGTH = 2000


def record_missed_announcement(
    agent_name: str,
    text: str,
    reason: str,
    *,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """Persist one undeliverable announcement and return the stored entry."""
    entry = {
        "id": f"missed-{uuid.uuid4().hex}",
        "agent_name": (agent_name or "").strip(),
        "text": (text or "").strip()[:MAX_TEXT_LENGTH],
        "reason": (reason or "").strip() or "unknown",
        "created_at": _now_iso(),
    }
    store = _read_store(path)
    announcements = [entry, *_entries(store)][:MAX_MISSED_ANNOUNCEMENTS]
    _write_store({ANNOUNCEMENTS_KEY: announcements}, path)
    return entry


def list_missed_announcements(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Return stored undeliverable announcements, newest first."""
    return _entries(_read_store(path))


def clear_missed_announcements(
    ids: list[str] | None = None,
    *,
    path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Remove acknowledged announcements (all of them when ``ids`` is None)."""
    if ids is None:
        _write_store({ANNOUNCEMENTS_KEY: []}, path)
        return []
    remove = set(ids)
    remaining = [
        entry for entry in _entries(_read_store(path)) if entry.get("id") not in remove
    ]
    _write_store({ANNOUNCEMENTS_KEY: remaining}, path)
    return remaining


def _entries(store: dict[str, Any]) -> list[dict[str, Any]]:
    announcements = store.get(ANNOUNCEMENTS_KEY)
    if not isinstance(announcements, list):
        return []
    return [entry for entry in announcements if isinstance(entry, dict)]


def _store_path(path: Path | str | None) -> Path:
    return Path(path) if path is not None else MISSED_ANNOUNCEMENTS_JSON_PATH


def _read_store(path: Path | str | None) -> dict[str, Any]:
    store_path = _store_path(path)
    if not store_path.exists():
        return {ANNOUNCEMENTS_KEY: []}
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {ANNOUNCEMENTS_KEY: []}
    return data if isinstance(data, dict) else {ANNOUNCEMENTS_KEY: []}


def _write_store(payload: dict[str, Any], path: Path | str | None) -> None:
    store_path = _store_path(path)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=store_path.parent, delete=False
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, store_path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
