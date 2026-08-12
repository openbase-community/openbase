"""Short-lived cache for expensive thread-list reads."""

from __future__ import annotations

import threading
import time
from typing import Any

from asgiref.sync import async_to_sync

from openbase_coder_cli.thread_sync.models import ThreadInfo
from openbase_coder_cli.thread_sync.session_manager import ThreadListPage

THREAD_LIST_CACHE_TTL_SECONDS = 8.0

_cache_lock = threading.Lock()
_cached_threads: list[ThreadInfo] | None = None
_cached_at = 0.0
_cached_pages: dict[tuple[int, str | None], tuple[float, ThreadListPage]] = {}
_cached_thread_states: dict[str, tuple[float, ThreadInfo | None]] = {}


def get_cached_thread_list(manager: Any) -> list[ThreadInfo]:
    """Return a cached thread list and coalesce concurrent refreshes."""
    global _cached_at, _cached_threads

    now = time.monotonic()
    with _cache_lock:
        if (
            _cached_threads is not None
            and now - _cached_at < THREAD_LIST_CACHE_TTL_SECONDS
        ):
            return list(_cached_threads)

        threads = list(async_to_sync(manager.list_threads)())
        _cached_threads = threads
        _cached_at = time.monotonic()
        return list(threads)


def get_cached_thread_page(
    manager: Any,
    *,
    limit: int,
    cursor: str | None = None,
) -> ThreadListPage:
    """Return one cached thread page and coalesce concurrent refreshes."""
    global _cached_pages

    key = (limit, cursor)
    now = time.monotonic()
    with _cache_lock:
        cached = _cached_pages.get(key)
        if cached is not None:
            cached_at, cached_page = cached
            if now - cached_at < THREAD_LIST_CACHE_TTL_SECONDS:
                return ThreadListPage(
                    threads=list(cached_page.threads),
                    next_cursor=cached_page.next_cursor,
                )

        page = async_to_sync(manager.list_thread_page)(limit=limit, cursor=cursor)
        cached_page = ThreadListPage(
            threads=list(page.threads),
            next_cursor=page.next_cursor,
        )
        _cached_pages[key] = (time.monotonic(), cached_page)
        return ThreadListPage(
            threads=list(cached_page.threads),
            next_cursor=cached_page.next_cursor,
        )


def get_cached_thread_state(manager: Any, thread_id: str) -> ThreadInfo | None:
    """Return one cached thread snapshot and coalesce concurrent refreshes."""
    global _cached_thread_states

    now = time.monotonic()
    with _cache_lock:
        cached = _cached_thread_states.get(thread_id)
        if cached is not None:
            cached_at, cached_thread = cached
            if now - cached_at < THREAD_LIST_CACHE_TTL_SECONDS:
                return cached_thread

        thread = async_to_sync(manager.get_thread_state)(thread_id)
        _cached_thread_states[thread_id] = (time.monotonic(), thread)
        return thread


def invalidate_thread_list_cache() -> None:
    """Clear cached thread-list reads after thread mutations."""
    global _cached_at, _cached_pages, _cached_thread_states, _cached_threads

    with _cache_lock:
        _cached_threads = None
        _cached_at = 0.0
        _cached_pages = {}
        _cached_thread_states = {}
