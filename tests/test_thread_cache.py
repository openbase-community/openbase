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


def test_slow_miss_does_not_block_other_keys(monkeypatch) -> None:
    """A slow app-server read for one thread must not stall reads of another —
    the whole point of per-key single-flight (no global lock held during I/O)."""
    import threading

    thread_cache.invalidate_thread_list_cache()
    monkeypatch.setattr(thread_cache.time, "monotonic", lambda: 100.0)

    blocking = threading.Event()

    class SlowManager:
        async def get_thread_state(self, thread_id: str):
            if thread_id == "slow":
                blocking.wait(timeout=5)
            return SimpleNamespace(session_id=thread_id)

    manager = SlowManager()
    slow_started = threading.Event()

    def read_slow():
        slow_started.set()
        thread_cache.get_cached_thread_state(manager, "slow")

    slow_thread = threading.Thread(target=read_slow)
    slow_thread.start()
    slow_started.wait(timeout=5)

    # The fast key must return while "slow" is still blocked in its compute.
    fast = thread_cache.get_cached_thread_state(manager, "fast")
    assert fast.session_id == "fast"

    blocking.set()
    slow_thread.join(timeout=5)
    assert not slow_thread.is_alive()


def test_concurrent_misses_same_key_coalesce(monkeypatch) -> None:
    """Concurrent misses on the same key share one computation."""
    import threading

    thread_cache.invalidate_thread_list_cache()
    monkeypatch.setattr(thread_cache.time, "monotonic", lambda: 100.0)

    release = threading.Event()

    class CountingManager:
        def __init__(self) -> None:
            self.calls = 0

        async def get_thread_state(self, thread_id: str):
            self.calls += 1
            release.wait(timeout=5)
            return SimpleNamespace(session_id=thread_id)

    manager = CountingManager()
    results: list[object] = []

    def read():
        results.append(thread_cache.get_cached_thread_state(manager, "shared"))

    threads = [threading.Thread(target=read) for _ in range(4)]
    for thread in threads:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert manager.calls == 1
    assert len(results) == 4
