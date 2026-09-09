"""Server-authoritative notification store with read/unread state.

Notifications are one file-backed collection shared by every client
(console, desktop, iOS, Android), so marking an item read on any device
clears it everywhere. Producers rewrite the store file atomically; the
``ws/notifications/`` consumer watches the file to push snapshots.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openbase_coder_cli.cli.utils import get_data_dir

NOTIFICATIONS_FILE = "notifications.json"

KIND_THREAD = "thread"
KIND_REPORT = "report"
KIND_APPROVAL = "approval"
KIND_SYNC_CONFLICT = "sync_conflict"
VALID_KINDS = frozenset({KIND_THREAD, KIND_REPORT, KIND_APPROVAL, KIND_SYNC_CONFLICT})

MAX_NOTIFICATIONS = 500
DONE_RETENTION_DAYS = 30
DEFAULT_LIST_LIMIT = 100

_lock = threading.Lock()


def notification_id(kind: str, entity_id: str) -> str:
    return f"{kind}:{entity_id}"


def notifications_store_path() -> Path:
    return get_data_dir() / NOTIFICATIONS_FILE


def list_notifications(
    *,
    include_read: bool = True,
    limit: int = DEFAULT_LIST_LIMIT,
) -> dict[str, Any]:
    """Return notifications newest-first plus the live unread count."""
    with _lock:
        state = _read_state_unlocked()
    entries = [
        entry
        for entry in state["notifications"].values()
        if not entry.get("resolved_at")
    ]
    unread_count = sum(1 for entry in entries if not entry.get("read_at"))
    if not include_read:
        entries = [entry for entry in entries if not entry.get("read_at")]
    entries.sort(key=lambda entry: entry.get("created_at") or "", reverse=True)
    return {
        "notifications": entries[: max(limit, 0)],
        "unread_count": unread_count,
    }


def upsert_notification(
    kind: str,
    entity_id: str,
    *,
    title: str,
    body: str = "",
    thread_id: str | None = None,
    project_path: str | None = None,
    reopen_if_read: bool = True,
) -> dict[str, Any] | None:
    """Create a notification, or reopen the existing one for this entity.

    Returns the entry when it was newly created or newly reopened (callers
    use that as the "fire a push" signal); returns None when the entity
    already has a live unread notification.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid notification kind {kind!r}")
    entity = entity_id.strip()
    if not entity:
        raise ValueError("entity_id is required")

    note_id = notification_id(kind, entity)
    with _lock:
        state = _read_state_unlocked()
        existing = state["notifications"].get(note_id)
        if existing and not existing.get("read_at") and not existing.get("resolved_at"):
            return None
        if existing and not reopen_if_read:
            return None
        entry = {
            "id": note_id,
            "kind": kind,
            "entity_id": entity,
            "thread_id": thread_id,
            "project_path": project_path,
            "title": title,
            "body": body,
            "created_at": _utc_now(),
            "read_at": None,
            "resolved_at": None,
        }
        state["notifications"][note_id] = entry
        _write_state_unlocked(state)
        return dict(entry)


def mark_read(
    ids: list[str] | None = None,
    *,
    kind: str | None = None,
    entity_id: str | None = None,
) -> int:
    """Mark notifications read by id list or by (kind, entity_id)."""
    targets = set(ids or [])
    if kind and entity_id:
        targets.add(notification_id(kind, entity_id.strip()))
    if not targets:
        return 0
    return _set_read_unlocked_for(lambda entry: entry["id"] in targets)


def mark_all_read() -> int:
    return _set_read_unlocked_for(lambda _entry: True)


def resolve_notification(kind: str, entity_id: str) -> bool:
    """Resolve a notification whose underlying item no longer needs attention.

    Used when an approval is answered or a sync conflict is resolved on any
    device: the notification drops out of lists and unread counts everywhere.
    """
    note_id = notification_id(kind, entity_id.strip())
    now = _utc_now()
    with _lock:
        state = _read_state_unlocked()
        entry = state["notifications"].get(note_id)
        if not entry or entry.get("resolved_at"):
            return False
        entry["resolved_at"] = now
        if not entry.get("read_at"):
            entry["read_at"] = now
        _write_state_unlocked(state)
        return True


def unresolved_ids(kind: str) -> set[str]:
    """Entity ids of live (unresolved) notifications of one kind."""
    with _lock:
        state = _read_state_unlocked()
    return {
        entry["entity_id"]
        for entry in state["notifications"].values()
        if entry.get("kind") == kind and not entry.get("resolved_at")
    }


def get_report_watermark() -> tuple[float | None, bool]:
    """Return (last seen report mtime, whether the baseline has been taken)."""
    with _lock:
        watermarks = _read_state_unlocked()["watermarks"]
    mtime = watermarks.get("reports_last_seen_mtime")
    return (
        float(mtime) if isinstance(mtime, (int, float)) else None,
        bool(watermarks.get("reports_baselined_at")),
    )


def set_report_watermark(mtime: float) -> None:
    with _lock:
        state = _read_state_unlocked()
        state["watermarks"]["reports_last_seen_mtime"] = mtime
        state["watermarks"].setdefault("reports_baselined_at", _utc_now())
        _write_state_unlocked(state)


def _set_read_unlocked_for(matcher) -> int:
    now = _utc_now()
    changed = 0
    with _lock:
        state = _read_state_unlocked()
        for entry in state["notifications"].values():
            if entry.get("read_at") or entry.get("resolved_at"):
                continue
            if matcher(entry):
                entry["read_at"] = now
                changed += 1
        if changed:
            _write_state_unlocked(state)
    return changed


def _prune_unlocked(state: dict[str, Any]) -> None:
    cutoff = (datetime.now(UTC) - timedelta(days=DONE_RETENTION_DAYS)).isoformat()
    notifications = state["notifications"]
    for note_id in [
        note_id
        for note_id, entry in notifications.items()
        if (entry.get("read_at") or entry.get("resolved_at"))
        and (entry.get("read_at") or entry.get("resolved_at")) < cutoff
    ]:
        notifications.pop(note_id, None)
    if len(notifications) > MAX_NOTIFICATIONS:
        # Evict oldest done entries first, then oldest unread if still over.
        def eviction_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
            entry = item[1]
            done = bool(entry.get("read_at") or entry.get("resolved_at"))
            return (0 if done else 1, entry.get("created_at") or "")

        for note_id, _entry in sorted(notifications.items(), key=eviction_key)[
            : len(notifications) - MAX_NOTIFICATIONS
        ]:
            notifications.pop(note_id, None)


def _read_state_unlocked() -> dict[str, Any]:
    try:
        payload = json.loads(notifications_store_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return {"version": 1, "notifications": {}, "watermarks": {}}
    notifications = payload.get("notifications")
    watermarks = payload.get("watermarks")
    return {
        "version": 1,
        "notifications": {
            note_id: entry
            for note_id, entry in (
                notifications.items() if isinstance(notifications, dict) else []
            )
            if isinstance(entry, dict) and isinstance(note_id, str)
        },
        "watermarks": watermarks if isinstance(watermarks, dict) else {},
    }


def _write_state_unlocked(state: dict[str, Any]) -> None:
    _prune_unlocked(state)
    path = notifications_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as tmp:
        json.dump(state, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
