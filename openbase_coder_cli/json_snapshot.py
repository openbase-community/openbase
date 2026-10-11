"""Stat-keyed cache for small JSON state files read on hot paths.

Thread-list annotation and report sweeps read the same small state files
(tags, favorites, voice assignments, the Super Agents state file) once per
item — hundreds to thousands of parses per request or sweep, all of the same
unchanged bytes. ``JsonFileSnapshot`` parses a file once per on-disk version:
an unchanged file costs one ``stat``; a rewritten file (atomic replace or
in-place write) is re-read on the next access.

Cached values are shared between callers, so read paths must treat them as
read-only. Write paths read a private copy through ``read_fresh`` and call
``invalidate`` after writing so in-process readers never serve the pre-write
version within the same stat tick.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")

# (inode, size, mtime_ns, ctime_ns) — None when the file is missing.
_Signature = tuple[int, int, int, int] | None


class JsonFileSnapshot(Generic[T]):
    """Parse a JSON file once per on-disk version, keyed by path."""

    def __init__(self, parse: Callable[[Any], T]) -> None:
        # ``parse`` receives the decoded JSON payload, or None when the file
        # is missing, unreadable, or not valid JSON.
        self._parse = parse
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[_Signature, T]] = {}

    def get(self, path: Path) -> T:
        key = str(path)
        signature = _signature(path)
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and cached[0] == signature:
                return cached[1]
        value = self._parse(_load(path) if signature is not None else None)
        with self._lock:
            self._entries[key] = (signature, value)
        return value

    def read_fresh(self, path: Path) -> T:
        """Parse without consulting or populating the cache (write paths)."""
        return self._parse(_load(path))

    def invalidate(self, path: Path | None = None) -> None:
        with self._lock:
            if path is None:
                self._entries.clear()
            else:
                self._entries.pop(str(path), None)


def _signature(path: Path) -> _Signature:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
