"""Per-thread model override storage.

A thread's model override is the model chosen from the composer's model
switcher (console, iOS, Android). It applies to every subsequent turn of that
thread — including voice-dispatched turns — until changed or cleared, and it
always stays within the thread's own execution backend (same-backend
switching only; cross-backend moves go through thread continuations instead).

Overrides live in a small JSON file in the data directory so they survive
service restarts, and are read per turn so applying one never requires
restarting the Codex app server or any Openbase service. This module is
imported by both the API views and the thread-sync session manager, so it
must stay free of Django and thread_sync imports.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from openbase_coder_cli.cli.utils import get_data_dir

MODEL_OVERRIDES_FILE = "thread-model-overrides.json"

_lock = threading.Lock()


def get_thread_model_override(thread_id: str | None) -> str | None:
    """The stored model override for a thread, or None."""
    normalized = _normalize_thread_id(thread_id)
    if not normalized:
        return None
    value = _read_overrides().get(normalized)
    return value if isinstance(value, str) and value else None


def set_thread_model_override(thread_id: str, model: str | None) -> str | None:
    """Store (or clear, when model is None/empty) a thread's model override."""
    normalized = _normalize_thread_id(thread_id)
    if not normalized:
        raise ValueError("thread_id is required")
    cleaned = " ".join(model.split()) if isinstance(model, str) else ""
    with _lock:
        overrides = _read_overrides_unlocked()
        if cleaned:
            overrides[normalized] = cleaned
        else:
            overrides.pop(normalized, None)
        _write_overrides_unlocked(overrides)
    return cleaned or None


def _normalize_thread_id(thread_id: str | None) -> str | None:
    if not isinstance(thread_id, str):
        return None
    normalized = thread_id.strip()
    return normalized or None


def _overrides_path() -> Path:
    return get_data_dir() / MODEL_OVERRIDES_FILE


def _read_overrides() -> dict[str, str]:
    with _lock:
        return _read_overrides_unlocked()


def _read_overrides_unlocked() -> dict[str, str]:
    try:
        payload = json.loads(_overrides_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    threads = payload.get("threads") if isinstance(payload, dict) else None
    if not isinstance(threads, dict):
        return {}
    return {
        key: value
        for key, value in threads.items()
        if isinstance(key, str) and key.strip() and isinstance(value, str) and value
    }


def _write_overrides_unlocked(overrides: dict[str, str]) -> None:
    path = _overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"threads": overrides}
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)
