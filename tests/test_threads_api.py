from __future__ import annotations

# ruff: noqa: E402, I001

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django
from rest_framework.test import APIRequestFactory, force_authenticate

django.setup()

from openbase_coder_cli.thread_sync.models import ThreadInfo, ThreadStatus, TurnInfo
from openbase_coder_cli.thread_sync.session_manager import ThreadListPage
from openbase_coder_cli.openbase_coder_cli_app import threads as thread_views


class FakeThreadManager:
    def __init__(self, threads: list[ThreadInfo]) -> None:
        self._threads = threads
        self.page_calls: list[dict[str, str | int | None]] = []
        self.thread_state_calls: list[str] = []

    async def list_threads(self) -> list[ThreadInfo]:
        return self._threads

    async def list_thread_page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> ThreadListPage:
        self.page_calls.append({"limit": limit, "cursor": cursor})
        start = int(cursor or 0)
        next_index = start + limit
        next_cursor = str(next_index) if next_index < len(self._threads) else None
        return ThreadListPage(
            threads=self._threads[start:next_index],
            next_cursor=next_cursor,
        )

    async def get_thread_state(self, thread_id: str) -> ThreadInfo | None:
        self.thread_state_calls.append(thread_id)
        return next(
            (thread for thread in self._threads if thread.session_id == thread_id),
            None,
        )


def _thread(index: int) -> ThreadInfo:
    now = datetime(2026, 5, 28, 12, tzinfo=timezone.utc)
    updated_at = now - timedelta(minutes=index)
    return ThreadInfo(
        session_id=f"thread-{index:03d}",
        directory=f"/tmp/project-{index:03d}",
        created_at=updated_at,
        updated_at=updated_at,
    )


def _get_threads(monkeypatch, url: str, threads: list[ThreadInfo]):
    thread_views.invalidate_thread_list_cache()
    manager = FakeThreadManager(threads)
    monkeypatch.setattr(
        thread_views,
        "get_session_manager",
        lambda: manager,
    )
    monkeypatch.setattr(thread_views, "get_livekit_shared_thread_id", lambda: None)
    monkeypatch.setattr(thread_views, "_refresh_projects_from_threads", lambda _: None)

    factory = APIRequestFactory()
    request = factory.get(url)
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return thread_views.thread_list(request), manager


def test_thread_list_returns_default_first_page(monkeypatch) -> None:
    response, manager = _get_threads(
        monkeypatch,
        "/api/threads/",
        [_thread(index) for index in range(30)],
    )

    assert response.status_code == 200
    assert response.data["count"] == 26
    assert response.data["page"] == 1
    assert response.data["page_size"] == 25
    assert response.data["previous"] is None
    assert response.data["next"] == "/api/threads/?page=2&page_size=25&cursor=25"
    assert len(response.data["threads"]) == 25
    assert response.data["threads"][0]["thread_id"] == "thread-000"
    assert manager.page_calls == [{"limit": 25, "cursor": None}]


def test_thread_list_slices_requested_page(monkeypatch) -> None:
    response, manager = _get_threads(
        monkeypatch,
        "/api/threads/?page=2&page_size=10&cursor=10",
        [_thread(index) for index in range(25)],
    )

    assert response.status_code == 200
    assert response.data["count"] == 21
    assert response.data["page"] == 2
    assert response.data["page_size"] == 10
    assert response.data["previous"] == "/api/threads/?page=1&page_size=10"
    assert response.data["next"] == "/api/threads/?page=3&page_size=10&cursor=20"
    assert [thread["thread_id"] for thread in response.data["threads"]] == [
        f"thread-{index:03d}" for index in range(10, 20)
    ]
    assert manager.page_calls == [{"limit": 10, "cursor": "10"}]


def test_thread_list_rejects_invalid_pagination(monkeypatch) -> None:
    response, _ = _get_threads(
        monkeypatch,
        "/api/threads/?page=0&page_size=10",
        [_thread(index) for index in range(3)],
    )

    assert response.status_code == 400
    assert response.data["error"] == "page must be a positive integer"


def test_thread_list_caps_page_size(monkeypatch) -> None:
    response, _ = _get_threads(
        monkeypatch,
        "/api/threads/?page_size=500",
        [_thread(index) for index in range(125)],
    )

    assert response.status_code == 200
    assert response.data["page_size"] == 100
    assert len(response.data["threads"]) == 100
    assert response.data["next"] == "/api/threads/?page_size=100&page=2&cursor=100"


def test_thread_list_skips_unavailable_livekit_fallback(monkeypatch) -> None:
    thread_views.invalidate_thread_list_cache()
    monkeypatch.setattr(
        thread_views,
        "get_session_manager",
        lambda: FakeThreadManager([_thread(1)]),
    )
    monkeypatch.setattr(
        thread_views,
        "get_livekit_shared_thread_id",
        lambda: "missing-dispatcher-thread",
    )
    monkeypatch.setattr(thread_views, "_refresh_projects_from_threads", lambda _: None)

    factory = APIRequestFactory()
    request = factory.get("/api/threads/?page_size=25")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = thread_views.thread_list(request)

    assert response.status_code == 200
    assert response.data["count"] == 1
    assert [thread["thread_id"] for thread in response.data["threads"]] == [
        "thread-001"
    ]


def test_thread_dispatcher_returns_cached_dispatcher_thread(monkeypatch) -> None:
    thread_views.invalidate_thread_list_cache()
    dispatcher_thread = _thread(1)
    manager = FakeThreadManager([dispatcher_thread, _thread(2)])
    monkeypatch.setattr(thread_views, "get_session_manager", lambda: manager)
    monkeypatch.setattr(
        thread_views,
        "get_livekit_shared_thread_id",
        lambda: dispatcher_thread.session_id,
    )

    factory = APIRequestFactory()
    request = factory.get("/api/threads/dispatcher/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    first_response = thread_views.thread_dispatcher(request)
    second_response = thread_views.thread_dispatcher(request)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.data["thread_id"] == dispatcher_thread.session_id
    assert manager.page_calls == []
    assert manager.thread_state_calls == [dispatcher_thread.session_id]


def test_thread_dispatcher_warms_missing_dispatcher(monkeypatch) -> None:
    thread_views.invalidate_thread_list_cache()
    dispatcher_thread = _thread(1)
    manager = FakeThreadManager([dispatcher_thread])
    monkeypatch.setattr(thread_views, "get_session_manager", lambda: manager)
    monkeypatch.setattr(thread_views, "get_livekit_shared_thread_id", lambda: None)

    warmed: list[bool] = []

    async def fake_warm_livekit_dispatcher_thread(**_kwargs) -> str:
        warmed.append(True)
        return dispatcher_thread.session_id

    monkeypatch.setattr(
        thread_views,
        "warm_livekit_dispatcher_thread",
        fake_warm_livekit_dispatcher_thread,
    )

    factory = APIRequestFactory()
    request = factory.get("/api/threads/dispatcher/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = thread_views.thread_dispatcher(request)

    assert response.status_code == 200
    assert response.data["thread_id"] == dispatcher_thread.session_id
    assert warmed == [True]
    assert manager.page_calls == []
    assert manager.thread_state_calls == [dispatcher_thread.session_id]


def test_thread_active_voice_returns_active_target_with_turn_text(monkeypatch) -> None:
    thread_views.invalidate_thread_list_cache()
    active_thread = _thread(3)
    active_thread.current_run = TurnInfo(
        run_id="turn-1",
        started_at=datetime(2026, 5, 28, 12, tzinfo=timezone.utc),
        status=ThreadStatus.running,
        accumulated_output="Earlier update.\n\nLatest voice response.",
        message="Do the thing",
    )
    manager = FakeThreadManager([_thread(1), active_thread])
    monkeypatch.setattr(thread_views, "get_session_manager", lambda: manager)
    monkeypatch.setattr(
        thread_views,
        "get_livekit_active_voice_thread_id",
        lambda: active_thread.session_id,
    )

    factory = APIRequestFactory()
    request = factory.get("/api/threads/active-voice/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = thread_views.thread_active_voice(request)

    assert response.status_code == 200
    assert response.data["thread_id"] == active_thread.session_id
    assert response.data["current_turn"]["accumulated_output"].endswith(
        "Latest voice response."
    )
    active_thread.current_run.accumulated_output = (
        "Earlier update.\n\nFresh call page response."
    )
    second_response = thread_views.thread_active_voice(request)
    assert second_response.status_code == 200
    assert second_response.data["current_turn"]["accumulated_output"].endswith(
        "Fresh call page response."
    )
    assert manager.page_calls == []
    assert manager.thread_state_calls == [
        active_thread.session_id,
        active_thread.session_id,
    ]


def test_thread_active_voice_warms_dispatcher_without_active_thread(monkeypatch) -> None:
    thread_views.invalidate_thread_list_cache()
    dispatcher_thread = _thread(1)
    manager = FakeThreadManager([dispatcher_thread])
    monkeypatch.setattr(thread_views, "get_session_manager", lambda: manager)
    monkeypatch.setattr(
        thread_views, "get_livekit_active_voice_thread_id", lambda: None
    )
    monkeypatch.setattr(thread_views, "get_livekit_shared_thread_id", lambda: None)

    warmed: list[bool] = []

    async def fake_warm_livekit_dispatcher_thread(**_kwargs) -> str:
        warmed.append(True)
        return dispatcher_thread.session_id

    monkeypatch.setattr(
        thread_views,
        "warm_livekit_dispatcher_thread",
        fake_warm_livekit_dispatcher_thread,
    )

    factory = APIRequestFactory()
    request = factory.get("/api/threads/active-voice/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = thread_views.thread_active_voice(request)

    assert response.status_code == 200
    assert response.data["thread_id"] == dispatcher_thread.session_id
    assert warmed == [True]
    assert manager.page_calls == []
    assert manager.thread_state_calls == [dispatcher_thread.session_id]


def test_thread_list_filters_favorites_without_changing_default_order(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    thread_views.set_thread_favorite("thread-002", True)
    thread_views.set_thread_favorite("thread-004", True)
    threads = [_thread(index) for index in range(6)]

    default_response, _ = _get_threads(monkeypatch, "/api/threads/", threads)
    favorite_response, manager = _get_threads(
        monkeypatch,
        "/api/threads/?favorite=true&page_size=1",
        threads,
    )

    assert [thread["thread_id"] for thread in default_response.data["threads"][:6]] == [
        f"thread-{index:03d}" for index in range(6)
    ]
    assert favorite_response.status_code == 200
    assert favorite_response.data["count"] == 2
    assert (
        favorite_response.data["next"]
        == "/api/threads/?favorite=true&page_size=1&page=2"
    )
    assert [thread["thread_id"] for thread in favorite_response.data["threads"]] == [
        "thread-002"
    ]
    assert favorite_response.data["threads"][0]["is_favorite"] is True
    assert manager.page_calls == []


def test_thread_list_filters_non_favorites(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    thread_views.set_thread_favorite("thread-001", True)

    response, _ = _get_threads(
        monkeypatch,
        "/api/threads/?favorite=false&page_size=10",
        [_thread(index) for index in range(3)],
    )

    assert response.status_code == 200
    assert [thread["thread_id"] for thread in response.data["threads"]] == [
        "thread-000",
        "thread-002",
    ]


def test_thread_favorite_endpoint_sets_and_clears_favorite(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    thread_views.invalidate_thread_list_cache()
    factory = APIRequestFactory()

    request = factory.patch(
        "/api/threads/thread-001/favorite/",
        {"is_favorite": True},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = thread_views.thread_favorite(request, "thread-001")

    assert response.status_code == 200
    assert response.data["thread_id"] == "thread-001"
    assert response.data["is_favorite"] is True
    assert response.data["favorited_at"]

    get_request = factory.get("/api/threads/thread-001/favorite/")
    force_authenticate(get_request, user=SimpleNamespace(is_authenticated=True))
    get_response = thread_views.thread_favorite(get_request, "thread-001")
    assert get_response.data["is_favorite"] is True

    clear_request = factory.patch(
        "/api/threads/thread-001/favorite/",
        {"is_favorite": False},
        format="json",
    )
    force_authenticate(clear_request, user=SimpleNamespace(is_authenticated=True))
    clear_response = thread_views.thread_favorite(clear_request, "thread-001")
    assert clear_response.data == {
        "thread_id": "thread-001",
        "is_favorite": False,
        "favorited_at": None,
    }


def test_thread_favorite_endpoint_rejects_non_boolean(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    factory = APIRequestFactory()
    request = factory.patch(
        "/api/threads/thread-001/favorite/",
        {"is_favorite": "true"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = thread_views.thread_favorite(request, "thread-001")

    assert response.status_code == 400
    assert response.data["error"] == "is_favorite must be a boolean"


def test_thread_tags_endpoint_sets_tags_and_lists_shared_options(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    thread_views.invalidate_thread_list_cache()
    factory = APIRequestFactory()

    request = factory.patch(
        "/api/threads/thread-001/tags/",
        {"tags": ["Needs Review", "client", "needs review"]},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = thread_views.thread_tags(request, "thread-001")

    assert response.status_code == 200
    assert response.data["thread_id"] == "thread-001"
    assert response.data["tags"] == ["Needs Review", "client"]
    assert [tag["label"] for tag in response.data["tag_options"]] == [
        "client",
        "Needs Review",
    ]

    get_request = factory.get("/api/threads/thread-001/tags/")
    force_authenticate(get_request, user=SimpleNamespace(is_authenticated=True))
    get_response = thread_views.thread_tags(get_request, "thread-001")

    assert get_response.data["tags"] == ["Needs Review", "client"]


def test_thread_tags_endpoint_rejects_non_list(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    factory = APIRequestFactory()
    request = factory.patch(
        "/api/threads/thread-001/tags/",
        {"tags": "client"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = thread_views.thread_tags(request, "thread-001")

    assert response.status_code == 400
    assert response.data["error"] == "tags must be a list"


def _activity_response(monkeypatch, threads: list[ThreadInfo]):
    thread_views.invalidate_thread_list_cache()
    manager = FakeThreadManager(threads)
    monkeypatch.setattr(thread_views, "get_session_manager", lambda: manager)

    factory = APIRequestFactory()
    request = factory.get("/api/threads/activity/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return thread_views.thread_activity(request)


def test_thread_activity_counts_only_recently_progressing_runs(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    threads = [_thread(index) for index in range(4)]
    threads[0].raw_status = ThreadStatus.running
    threads[0].updated_at = now - timedelta(minutes=1)
    threads[1].raw_status = ThreadStatus.waiting
    threads[1].updated_at = now
    threads[2].raw_status = ThreadStatus.running
    threads[2].updated_at = (
        now - thread_views.RUN_ACTIVITY_FRESHNESS - timedelta(seconds=1)
    )

    response = _activity_response(monkeypatch, threads)

    assert response.status_code == 200
    assert response.data["active_run_count"] == 1
    assert response.data["thread_count"] == 4


def test_thread_activity_reports_zero_when_no_runs_active(monkeypatch) -> None:
    response = _activity_response(monkeypatch, [_thread(0), _thread(1)])

    assert response.status_code == 200
    assert response.data["active_run_count"] == 0
    assert response.data["thread_count"] == 2
