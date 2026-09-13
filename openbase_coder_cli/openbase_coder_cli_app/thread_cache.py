"""Short-lived cache for expensive thread-list reads."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Hashable, TypeVar

from asgiref.sync import async_to_sync

from openbase_coder_cli.thread_sync.models import ThreadInfo
from openbase_coder_cli.thread_sync.session_manager import ThreadListPage

THREAD_LIST_CACHE_TTL_SECONDS = 8.0

_T = TypeVar("_T")


class _InFlight:
    """A computation in progress, shared with everyone waiting on the same key."""

    __slots__ = ("event", "value", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None


class _SingleFlightCache:
    """Per-key TTL cache that coalesces concurrent misses onto one computation
    without holding a lock while that computation runs.

    The reads this backs go to the single out-of-process app-server connection.
    A previous design held one global lock across that (slow) call on every
    miss, so a single slow round-trip serialized every thread read. Here the
    lock only guards the small bookkeeping dicts; the value is computed outside
    it, so a miss on one key never blocks reads of another key, while concurrent
    misses on the *same* key still share one call and its result (or error).
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[Hashable, tuple[float, Any]] = {}
        self._inflight: dict[Hashable, _InFlight] = {}

    def get(self, key: Hashable, compute: Callable[[], _T]) -> _T:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and time.monotonic() - entry[0] < self._ttl:
                return entry[1]
            inflight = self._inflight.get(key)
            leader = inflight is None
            if leader:
                inflight = _InFlight()
                self._inflight[key] = inflight

        if not leader:
            # Wait for the in-flight leader and share its result — never hold a
            # lock or issue a duplicate app-server call.
            inflight.event.wait()
            if inflight.error is not None:
                raise inflight.error
            return inflight.value

        try:
            value = compute()
        except BaseException as error:  # noqa: BLE001 - re-raised to all waiters
            inflight.error = error
            with self._lock:
                self._inflight.pop(key, None)
            inflight.event.set()
            raise
        with self._lock:
            self._entries[key] = (time.monotonic(), value)
            self._inflight.pop(key, None)
        inflight.value = value
        inflight.event.set()
        return value

    def invalidate(self) -> None:
        with self._lock:
            self._entries.clear()


_thread_cache = _SingleFlightCache(THREAD_LIST_CACHE_TTL_SECONDS)


def get_cached_thread_list(manager: Any) -> list[ThreadInfo]:
    """Return a cached thread list and coalesce concurrent refreshes."""
    threads = _thread_cache.get(
        ("list",),
        lambda: list(async_to_sync(manager.list_threads)()),
    )
    return list(threads)


def get_cached_thread_page(
    manager: Any,
    *,
    limit: int,
    cursor: str | None = None,
) -> ThreadListPage:
    """Return one cached thread page and coalesce concurrent refreshes."""

    def _compute() -> ThreadListPage:
        page = async_to_sync(manager.list_thread_page)(limit=limit, cursor=cursor)
        return ThreadListPage(threads=list(page.threads), next_cursor=page.next_cursor)

    page = _thread_cache.get(("page", limit, cursor), _compute)
    return ThreadListPage(threads=list(page.threads), next_cursor=page.next_cursor)


def get_cached_thread_state(manager: Any, thread_id: str) -> ThreadInfo | None:
    """Return one cached thread snapshot and coalesce concurrent refreshes."""
    return _thread_cache.get(
        ("state", thread_id),
        lambda: async_to_sync(manager.get_thread_state)(thread_id),
    )


def invalidate_thread_list_cache() -> None:
    """Clear cached thread-list reads after thread mutations."""
    _thread_cache.invalidate()
