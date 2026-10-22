"""Persisted categorization state: categories, per-person assignments, and the
cached identity index. Lives in the Openbase data directory as one JSON file."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openbase_coder_cli.cli.utils import get_data_dir

STORE_FILE = "messaging-contacts.json"
STORE_VERSION = 1

CATEGORY_POLICIES = ("ask", "block")

# Proposed default set; editable through the API. "block" auto-declines agent
# approval requests that name the contact, "ask" keeps the normal approval flow.
DEFAULT_CATEGORIES: tuple[dict[str, str], ...] = (
    {"id": "family", "label": "Family", "policy": "ask"},
    {"id": "friends", "label": "Friends", "policy": "ask"},
    {"id": "work", "label": "Work", "policy": "ask"},
    {"id": "services", "label": "Services & businesses", "policy": "ask"},
    {"id": "unknown", "label": "Unknown or spam", "policy": "block"},
)

_lock = threading.Lock()


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def store_path() -> Path:
    return get_data_dir() / STORE_FILE


def read_store() -> dict[str, Any]:
    with _lock:
        return _read_unlocked()


def update_store(mutate) -> dict[str, Any]:
    """Apply ``mutate(store)`` under the lock and persist the result."""
    with _lock:
        store = _read_unlocked()
        mutate(store)
        _write_unlocked(store)
        return store


def categories(store: dict[str, Any]) -> list[dict[str, Any]]:
    raw = store.get("categories")
    if not isinstance(raw, list):
        return [dict(item) for item in DEFAULT_CATEGORIES]
    return [item for item in raw if isinstance(item, dict) and item.get("id")]


def people_state(store: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = store.get("people")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def index(store: dict[str, Any]) -> dict[str, Any]:
    raw = store.get("index")
    return raw if isinstance(raw, dict) else {}


def sources_config(store: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw = store.get("sources")
    return raw if isinstance(raw, list) else None


def _read_unlocked() -> dict[str, Any]:
    path = store_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"version": STORE_VERSION}
    return payload if isinstance(payload, dict) else {"version": STORE_VERSION}


def _write_unlocked(store: dict[str, Any]) -> None:
    path = store_path()
    store["version"] = STORE_VERSION
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{STORE_FILE}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(store, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
