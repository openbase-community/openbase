from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace


def _setup_django():
    os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings"
    )

    import django

    django.setup()


class FakeManager:
    async def get_thread_state(self, thread_id: str):
        return None

    async def list_threads(self):
        return [
            SimpleNamespace(
                session_id="thread-1",
                name="build-thread",
                agent_name="Build Agent",
                title=None,
                preview=None,
                directory="/tmp/build",
            )
        ]


def test_transfer_route_resolves_agent_name(monkeypatch):
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import livekit as views

    calls = []

    async def fake_publish_transfer_to_thread(thread_id, **kwargs):
        calls.append((thread_id, kwargs))
        return SimpleNamespace(
            command_id="route-1",
            room_name="room-1",
            state=SimpleNamespace(
                dispatcher_thread_id="dispatcher-1",
                dispatcher_voice_id="dispatcher-voice",
                dispatcher_voice_name="Jacqueline",
                active_target_thread_id=thread_id,
                active_target_voice_id="voice-1",
                active_target_voice_name="Alice",
                active_route="target",
            ),
        )

    monkeypatch.setattr(views, "get_session_manager", lambda: FakeManager())
    monkeypatch.setattr(
        views, "publish_transfer_to_thread", fake_publish_transfer_to_thread
    )

    factory = APIRequestFactory()
    request = factory.post(
        "/api/livekit-voice-route/transfer/",
        {"agent_name": "Build Agent"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.livekit_voice_route_transfer(request)

    assert response.status_code == 202
    assert calls == [
        (
            "thread-1",
            {
                "directory": "/tmp/build",
                "label": "build-thread",
                "agent_name": "Build Agent",
                "room_name": None,
            },
        )
    ]
    assert response.data["state"]["active_target_thread_id"] == "thread-1"


def test_transfer_route_selects_latest_matching_agent_name(monkeypatch):
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import livekit as views

    class AmbiguousManager:
        async def list_threads(self):
            return [
                SimpleNamespace(
                    session_id="thread-1",
                    name="build-thread-one",
                    agent_name="Build Agent",
                    title=None,
                    preview=None,
                    directory="/tmp/one",
                    updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                SimpleNamespace(
                    session_id="thread-2",
                    name="build-thread-two",
                    agent_name="build agent",
                    title=None,
                    preview=None,
                    directory="/tmp/two",
                    updated_at=datetime(2026, 1, 2, tzinfo=UTC),
                ),
            ]

    calls = []

    async def fake_publish_transfer_to_thread(thread_id, **kwargs):
        calls.append((thread_id, kwargs))
        return SimpleNamespace(
            command_id="route-1",
            room_name="room-1",
            state=SimpleNamespace(
                dispatcher_thread_id="dispatcher-1",
                dispatcher_voice_id="dispatcher-voice",
                dispatcher_voice_name="Jacqueline",
                active_target_thread_id=thread_id,
                active_target_voice_id="voice-1",
                active_target_voice_name="Alice",
                active_route="target",
            ),
        )

    monkeypatch.setattr(views, "get_session_manager", lambda: AmbiguousManager())
    monkeypatch.setattr(
        views, "publish_transfer_to_thread", fake_publish_transfer_to_thread
    )

    factory = APIRequestFactory()
    request = factory.post(
        "/api/livekit-voice-route/transfer/",
        {"agent_name": "BUILD AGENT"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.livekit_voice_route_transfer(request)

    assert response.status_code == 202
    assert calls == [
        (
            "thread-2",
            {
                "directory": "/tmp/two",
                "label": "build-thread-two",
                "agent_name": "BUILD AGENT",
                "room_name": None,
            },
        )
    ]
    assert response.data["state"]["active_target_thread_id"] == "thread-2"


def test_transfer_route_selects_newest_uuidv7_matching_agent_name(monkeypatch):
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import livekit as views

    class AmbiguousManager:
        async def list_threads(self):
            return [
                SimpleNamespace(
                    session_id="019fe1f6-9716-7163-aba0-7fe677f3ab75",
                    name="older-george",
                    agent_name="George",
                    title=None,
                    preview=None,
                    directory="/tmp/old",
                ),
                SimpleNamespace(
                    session_id="019fe3ff-bd84-7ce2-8745-6397697ae978",
                    name="newer-george",
                    agent_name="George",
                    title=None,
                    preview=None,
                    directory="/tmp/new",
                ),
            ]

    calls = []

    async def fake_publish_transfer_to_thread(thread_id, **kwargs):
        calls.append((thread_id, kwargs))
        return SimpleNamespace(
            command_id="route-1",
            room_name="room-1",
            state=SimpleNamespace(
                dispatcher_thread_id="dispatcher-1",
                dispatcher_voice_id="dispatcher-voice",
                dispatcher_voice_name="Jacqueline",
                active_target_thread_id=thread_id,
                active_target_voice_id="voice-george",
                active_target_voice_name="George",
                active_route="target",
            ),
        )

    monkeypatch.setattr(views, "get_session_manager", lambda: AmbiguousManager())
    monkeypatch.setattr(
        views, "publish_transfer_to_thread", fake_publish_transfer_to_thread
    )

    factory = APIRequestFactory()
    request = factory.post(
        "/api/livekit-voice-route/transfer/",
        {"agent_name": "George"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.livekit_voice_route_transfer(request)

    assert response.status_code == 202
    assert calls == [
        (
            "019fe3ff-bd84-7ce2-8745-6397697ae978",
            {
                "directory": "/tmp/new",
                "label": "newer-george",
                "agent_name": "George",
                "room_name": None,
            },
        )
    ]
    assert (
        response.data["state"]["active_target_thread_id"]
        == "019fe3ff-bd84-7ce2-8745-6397697ae978"
    )


def test_transfer_route_selects_latest_created_matching_agent_name(monkeypatch):
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import livekit as views

    class AmbiguousManager:
        async def list_threads(self):
            return [
                SimpleNamespace(
                    session_id="thread-1",
                    name="build-thread-one",
                    agent_name="Build Agent",
                    title=None,
                    preview=None,
                    directory="/tmp/one",
                    updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                SimpleNamespace(
                    session_id="thread-2",
                    name="build-thread-two",
                    agent_name="Build Agent",
                    title=None,
                    preview=None,
                    directory="/tmp/two",
                    updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                    created_at=datetime(2026, 1, 2, tzinfo=UTC),
                ),
            ]

    calls = []

    async def fake_publish_transfer_to_thread(thread_id, **kwargs):
        calls.append((thread_id, kwargs))
        return SimpleNamespace(
            command_id="route-1",
            room_name="room-1",
            state=SimpleNamespace(
                dispatcher_thread_id="dispatcher-1",
                dispatcher_voice_id="dispatcher-voice",
                dispatcher_voice_name="Jacqueline",
                active_target_thread_id=thread_id,
                active_target_voice_id="voice-1",
                active_target_voice_name="Alice",
                active_route="target",
            ),
        )

    monkeypatch.setattr(views, "get_session_manager", lambda: AmbiguousManager())
    monkeypatch.setattr(
        views, "publish_transfer_to_thread", fake_publish_transfer_to_thread
    )

    factory = APIRequestFactory()
    request = factory.post(
        "/api/livekit-voice-route/transfer/",
        {"agent_name": "Build Agent"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.livekit_voice_route_transfer(request)

    assert response.status_code == 202
    assert calls == [
        (
            "thread-2",
            {
                "directory": "/tmp/two",
                "label": "build-thread-two",
                "agent_name": "Build Agent",
                "room_name": None,
            },
        )
    ]
    assert response.data["state"]["active_target_thread_id"] == "thread-2"


def test_transfer_route_resolves_derived_agent_name_for_named_thread(monkeypatch):
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import livekit as views

    class DerivedNameManager:
        async def list_threads(self):
            return [
                SimpleNamespace(
                    session_id="thread-1",
                    name="Build Feature",
                    agent_name=None,
                    title=None,
                    preview=None,
                    directory="/tmp/build",
                    updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            ]

    calls = []

    async def fake_publish_transfer_to_thread(thread_id, **kwargs):
        calls.append((thread_id, kwargs))
        return SimpleNamespace(
            command_id="route-1",
            room_name="room-1",
            state=SimpleNamespace(
                dispatcher_thread_id="dispatcher-1",
                dispatcher_voice_id="dispatcher-voice",
                dispatcher_voice_name="Jacqueline",
                active_target_thread_id=thread_id,
                active_target_voice_id="voice-dottie",
                active_target_voice_name="Dottie",
                active_route="target",
            ),
        )

    monkeypatch.setattr(views, "get_session_manager", lambda: DerivedNameManager())
    monkeypatch.setattr(
        views,
        "super_agent_voice_for_context",
        lambda thread_id, label: SimpleNamespace(name="Dottie"),
    )
    monkeypatch.setattr(
        views, "publish_transfer_to_thread", fake_publish_transfer_to_thread
    )

    request = APIRequestFactory().post(
        "/api/livekit-voice-route/transfer/",
        {"agent_name": "Dottie"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.livekit_voice_route_transfer(request)

    assert response.status_code == 202
    assert calls == [
        (
            "thread-1",
            {
                "directory": "/tmp/build",
                "label": "Build Feature",
                "agent_name": "Dottie",
                "room_name": None,
            },
        )
    ]


def test_transfer_route_does_not_match_dispatcher_by_agent_name(monkeypatch):
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import livekit as views

    class DispatcherCollisionManager:
        async def list_threads(self):
            return [
                SimpleNamespace(
                    session_id="dispatcher-1",
                    name="dispatcher",
                    agent_name="Dottie",
                    title=None,
                    preview=None,
                    directory="/tmp/dispatcher",
                    updated_at=datetime(2026, 1, 3, tzinfo=UTC),
                ),
                SimpleNamespace(
                    session_id="thread-1",
                    name="Build Feature",
                    agent_name=None,
                    title=None,
                    preview=None,
                    directory="/tmp/build",
                    updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ]

    calls = []

    async def fake_publish_transfer_to_thread(thread_id, **kwargs):
        calls.append((thread_id, kwargs))
        return SimpleNamespace(
            command_id="route-1",
            room_name="room-1",
            state=SimpleNamespace(
                dispatcher_thread_id="dispatcher-1",
                dispatcher_voice_id="dispatcher-voice",
                dispatcher_voice_name="Jacqueline",
                active_target_thread_id=thread_id,
                active_target_voice_id="voice-dottie",
                active_target_voice_name="Dottie",
                active_route="target",
            ),
        )

    monkeypatch.setattr(
        views, "get_session_manager", lambda: DispatcherCollisionManager()
    )
    monkeypatch.setattr(
        views,
        "get_livekit_voice_route_state",
        lambda: SimpleNamespace(dispatcher_thread_id="dispatcher-1"),
    )
    monkeypatch.setattr(
        views,
        "super_agent_voice_for_context",
        lambda thread_id, label: SimpleNamespace(name="Dottie"),
    )
    monkeypatch.setattr(
        views, "publish_transfer_to_thread", fake_publish_transfer_to_thread
    )

    request = APIRequestFactory().post(
        "/api/livekit-voice-route/transfer/",
        {"agent_name": "Dottie"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.livekit_voice_route_transfer(request)

    assert response.status_code == 202
    assert calls == [
        (
            "thread-1",
            {
                "directory": "/tmp/build",
                "label": "Build Feature",
                "agent_name": "Dottie",
                "room_name": None,
            },
        )
    ]


def test_transfer_route_passes_thread_agent_name_for_thread_id(monkeypatch):
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import livekit as views

    calls = []

    class ThreadManager:
        async def get_thread_state(self, thread_id: str):
            return SimpleNamespace(
                session_id=thread_id,
                name="create-lorem-read-me",
                agent_name="Dorothy",
                title=None,
                preview=None,
                directory="/tmp/project",
            )

    async def fake_publish_transfer_to_thread(thread_id, **kwargs):
        calls.append((thread_id, kwargs))
        return SimpleNamespace(
            command_id="route-1",
            room_name="room-1",
            state=SimpleNamespace(
                dispatcher_thread_id="dispatcher-1",
                dispatcher_voice_id="dispatcher-voice",
                dispatcher_voice_name="Jacqueline",
                active_target_thread_id=thread_id,
                active_target_voice_id="voice-dorothy",
                active_target_voice_name="Dorothy",
                active_route="target",
            ),
        )

    monkeypatch.setattr(views, "get_session_manager", lambda: ThreadManager())
    monkeypatch.setattr(
        views, "publish_transfer_to_thread", fake_publish_transfer_to_thread
    )

    request = APIRequestFactory().post(
        "/api/livekit-voice-route/transfer/",
        {"thread_id": "thread-dorothy"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.livekit_voice_route_transfer(request)

    assert response.status_code == 202
    assert calls == [
        (
            "thread-dorothy",
            {
                "directory": "/tmp/project",
                "label": "create-lorem-read-me",
                "room_name": None,
                "agent_name": "Dorothy",
            },
        )
    ]


def test_transfer_route_rejects_dispatcher_thread_id(monkeypatch):
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import livekit as views

    calls = []

    class ThreadManager:
        async def get_thread_state(self, thread_id: str):
            return SimpleNamespace(
                session_id=thread_id,
                name="dispatcher",
                agent_name="Dottie",
                title=None,
                preview=None,
                directory="/tmp/dispatcher",
            )

    async def fake_publish_transfer_to_thread(thread_id, **kwargs):
        calls.append((thread_id, kwargs))

    monkeypatch.setattr(views, "get_session_manager", lambda: ThreadManager())
    monkeypatch.setattr(
        views,
        "get_livekit_voice_route_state",
        lambda: SimpleNamespace(dispatcher_thread_id="dispatcher-1"),
    )
    monkeypatch.setattr(
        views, "publish_transfer_to_thread", fake_publish_transfer_to_thread
    )

    request = APIRequestFactory().post(
        "/api/livekit-voice-route/transfer/",
        {"thread_id": "dispatcher-1"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.livekit_voice_route_transfer(request)

    assert response.status_code == 409
    assert "dispatcher thread cannot be used" in response.data["detail"]
    assert calls == []


def test_transfer_route_derives_agent_name_for_thread_id_without_metadata(monkeypatch):
    _setup_django()

    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import livekit as views

    calls = []

    class ThreadManager:
        async def get_thread_state(self, thread_id: str):
            return SimpleNamespace(
                session_id=thread_id,
                name="Build Feature",
                agent_name=None,
                title=None,
                preview=None,
                directory="/tmp/project",
            )

    async def fake_publish_transfer_to_thread(thread_id, **kwargs):
        calls.append((thread_id, kwargs))
        return SimpleNamespace(
            command_id="route-1",
            room_name="room-1",
            state=SimpleNamespace(
                dispatcher_thread_id="dispatcher-1",
                dispatcher_voice_id="dispatcher-voice",
                dispatcher_voice_name="Jacqueline",
                active_target_thread_id=thread_id,
                active_target_voice_id="voice-dottie",
                active_target_voice_name="Dottie",
                active_route="target",
            ),
        )

    monkeypatch.setattr(views, "get_session_manager", lambda: ThreadManager())
    monkeypatch.setattr(
        views,
        "super_agent_voice_for_context",
        lambda thread_id, label: SimpleNamespace(name="Dottie"),
    )
    monkeypatch.setattr(
        views, "publish_transfer_to_thread", fake_publish_transfer_to_thread
    )

    request = APIRequestFactory().post(
        "/api/livekit-voice-route/transfer/",
        {"thread_id": "thread-dottie"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.livekit_voice_route_transfer(request)

    assert response.status_code == 202
    assert calls == [
        (
            "thread-dottie",
            {
                "directory": "/tmp/project",
                "label": "Build Feature",
                "room_name": None,
                "agent_name": "Dottie",
            },
        )
    ]
