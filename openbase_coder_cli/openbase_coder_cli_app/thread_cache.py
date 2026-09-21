"""Short-lived cache for expensive thread-list reads."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Hashable, TypeVar

from asgiref.sync import async_to_sync

from openbase_coder_cli.thread_sync.models import ThreadInfo
from openbase_coder_cli.thread_sync.session_manager import ThreadListPage

THREAD_LIST_CACHE_TTL_SECONDS = 8.0
# During an active voice session, turn starts and steers invalidate this
# cache continuously (~2/s observed 2026-09-16), so every console poll
# recomputed the full recency window (5+ thread/list RPCs each) and
# saturated the app-server connection (~1,330 RPCs in 10 minutes).
# Invalidation therefore only marks entries stale; a value computed within
# this floor still serves during churn, bounding recomputes to ~1 per floor
# interval. Mutation responses carry their own fresh state, so the only cost
# is list polls trailing a mutation by at most this long.
THREAD_LIST_STALE_SERVE_SECONDS = 2.5

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

    def __init__(self, ttl_seconds: float, stale_serve_seconds: float = 0.0) -> None:
        self._ttl = ttl_seconds
        self._stale_serve = stale_serve_seconds
        self._lock = threading.Lock()
        # key -> (computed_at, value, stale). Invalidation flips stale rather
        # than dropping the entry so mutation churn cannot force a recompute
        # on every read (see THREAD_LIST_STALE_SERVE_SECONDS).
        self._entries: dict[Hashable, tuple[float, Any, bool]] = {}
        self._inflight: dict[Hashable, _InFlight] = {}

    def get(self, key: Hashable, compute: Callable[[], _T]) -> _T:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                computed_at, value, stale = entry
                age = time.monotonic() - computed_at
                if not stale and age < self._ttl:
                    return value
                if stale and age < self._stale_serve:
                    return value
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
            self._entries[key] = (time.monotonic(), value, False)
            self._inflight.pop(key, None)
        inflight.value = value
        inflight.event.set()
        return value

    def invalidate(self) -> None:
        with self._lock:
            self._entries = {
                key: (computed_at, value, True)
                for key, (computed_at, value, _stale) in self._entries.items()
            }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_thread_cache = _SingleFlightCache(
    THREAD_LIST_CACHE_TTL_SECONDS,
    stale_serve_seconds=THREAD_LIST_STALE_SERVE_SECONDS,
)


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


def get_cached_thread_history_page(
    manager: Any,
    thread_id: str,
    history_cursor: str,
) -> ThreadInfo | None:
    """Return one cached older-history page and coalesce concurrent refreshes.

    Older pages are effectively immutable (turns append at the head), but they
    are the most expensive reads — a not-loaded thread's page can force a full
    rollout-file parse — so re-opening a scrolled-back thread should not repeat
    that work within the TTL.
    """
    return _thread_cache.get(
        ("state", thread_id, history_cursor),
        lambda: async_to_sync(manager.get_thread_state)(
            thread_id,
            history_cursor=history_cursor,
        ),
    )


def invalidate_thread_list_cache() -> None:
    """Mark cached thread reads stale after thread mutations.

    Recently computed values still serve for a short floor (see
    THREAD_LIST_STALE_SERVE_SECONDS) so mutation churn during an active
    voice session cannot force a full recompute on every poll.
    """
    _thread_cache.invalidate()


def clear_thread_cache() -> None:
    """Drop every cached thread read outright (tests and hard resets)."""
    _thread_cache.clear()
