from __future__ import annotations

from types import SimpleNamespace

from openbase_coder_cli.openbase_coder_cli_app import thread_cache
from openbase_coder_cli.thread_sync.session_manager import ThreadListPage


class FakeThreadManager:
    def __init__(self) -> None:
        self.calls = 0

    async def list_threads(self):
        self.calls += 1
        return [SimpleNamespace(session_id=f"thread-{self.calls}")]

    async def list_thread_page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ):
        self.calls += 1
        return ThreadListPage(
            threads=[SimpleNamespace(session_id=f"thread-{self.calls}")],
            next_cursor=cursor,
        )

    async def get_thread_state(self, thread_id: str):
        self.calls += 1
        return SimpleNamespace(session_id=thread_id, call=self.calls)


def test_cached_thread_list_reuses_fresh_result(monkeypatch) -> None:
    thread_cache.invalidate_thread_list_cache()
    manager = FakeThreadManager()
    now = 100.0
    monkeypatch.setattr(thread_cache.time, "monotonic", lambda: now)

    first = thread_cache.get_cached_thread_list(manager)
    second = thread_cache.get_cached_thread_list(manager)

    assert manager.calls == 1
    assert first == second


def test_cached_thread_list_expires_after_ttl(monkeypatch) -> None:
    thread_cache.invalidate_thread_list_cache()
    manager = FakeThreadManager()
    now = 100.0
    monkeypatch.setattr(thread_cache.time, "monotonic", lambda: now)

    first = thread_cache.get_cached_thread_list(manager)
    now += thread_cache.THREAD_LIST_CACHE_TTL_SECONDS + 0.1
    second = thread_cache.get_cached_thread_list(manager)

    assert manager.calls == 2
    assert first != second


def test_invalidate_thread_list_cache_forces_refresh(monkeypatch) -> None:
    thread_cache.invalidate_thread_list_cache()
    manager = FakeThreadManager()
    monkeypatch.setattr(thread_cache.time, "monotonic", lambda: 100.0)

    first = thread_cache.get_cached_thread_list(manager)
    thread_cache.invalidate_thread_list_cache()
    second = thread_cache.get_cached_thread_list(manager)

    assert manager.calls == 2
    assert first != second


def test_cached_thread_page_reuses_fresh_result(monkeypatch) -> None:
    thread_cache.invalidate_thread_list_cache()
    manager = FakeThreadManager()
    monkeypatch.setattr(thread_cache.time, "monotonic", lambda: 100.0)

    first = thread_cache.get_cached_thread_page(manager, limit=25, cursor=None)
    second = thread_cache.get_cached_thread_page(manager, limit=25, cursor=None)

    assert manager.calls == 1
    assert first == second


def test_cached_thread_page_cache_key_includes_cursor(monkeypatch) -> None:
    thread_cache.invalidate_thread_list_cache()
    manager = FakeThreadManager()
    monkeypatch.setattr(thread_cache.time, "monotonic", lambda: 100.0)

    first = thread_cache.get_cached_thread_page(manager, limit=25, cursor=None)
    second = thread_cache.get_cached_thread_page(manager, limit=25, cursor="cursor-2")

    assert manager.calls == 2
    assert first != second


def test_cached_thread_state_reuses_fresh_result(monkeypatch) -> None:
    thread_cache.invalidate_thread_list_cache()
    manager = FakeThreadManager()
    monkeypatch.setattr(thread_cache.time, "monotonic", lambda: 100.0)

    first = thread_cache.get_cached_thread_state(manager, "dispatcher-thread")
    second = thread_cache.get_cached_thread_state(manager, "dispatcher-thread")

    assert manager.calls == 1
    assert first == second


def test_cached_thread_state_cache_key_includes_thread_id(monkeypatch) -> None:
    thread_cache.invalidate_thread_list_cache()
    manager = FakeThreadManager()
    monkeypatch.setattr(thread_cache.time, "monotonic", lambda: 100.0)

    first = thread_cache.get_cached_thread_state(manager, "dispatcher-thread")
    second = thread_cache.get_cached_thread_state(manager, "other-thread")

    assert manager.calls == 2
    assert first != second
