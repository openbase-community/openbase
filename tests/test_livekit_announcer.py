from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
import jwt  # noqa: E402
import livekit.api as livekit_api  # noqa: E402
import pytest  # noqa: E402
from django.test import Client  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli import dispatcher_config  # noqa: E402
from openbase_coder_cli.livekit_announcer import (  # noqa: E402
    ANNOUNCER_TOPIC,
    AUDIO_PLAYBACK_KIND,
    NoActiveLiveKitRoomError,
    publish_announcer_audio_file,
    publish_announcer_message,
)
from openbase_coder_cli.livekit_voice_history import (  # noqa: E402
    record_voice_assignment,  # noqa: E402
)
from openbase_coder_cli.openbase_coder_cli_app import views  # noqa: E402
from openbase_coder_cli.tts_providers import KOKORO_PROVIDER_ID  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_voice_config(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "SUPER_AGENTS_STATE_FILE",
        str(tmp_path / "missing-super-agents-state.json"),
    )
    monkeypatch.setattr(
        dispatcher_config,
        "selected_tts_provider_id",
        lambda path=None: "cartesia",
    )


class FakeRoomService:
    def __init__(self, rooms, participants_by_room):
        self._rooms = rooms
        self._participants_by_room = participants_by_room
        self.sent = []

    async def list_rooms(self, request):
        return SimpleNamespace(rooms=self._rooms)

    async def list_participants(self, request):
        return SimpleNamespace(participants=self._participants_by_room[request.room])

    async def send_data(self, request):
        self.sent.append(request)
        return SimpleNamespace()


class FakeLiveKitClient:
    def __init__(self, rooms, participants_by_room):
        self.room = FakeRoomService(rooms, participants_by_room)
        self.closed = False

    async def aclose(self):
        self.closed = True


def _room(name: str, created: int, participants: int = 2):
    return SimpleNamespace(
        name=name,
        creation_time_ms=created,
        creation_time=created // 1000,
        num_participants=participants,
    )


def _participant(identity: str, *, kind, state=livekit_api.ParticipantInfo.State.ACTIVE):
    return SimpleNamespace(identity=identity, kind=kind, state=state)


def test_publish_announcer_message_selects_latest_active_room(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    older = _room("room-old", 100)
    newer = _room("room-new", 200)
    client = FakeLiveKitClient(
        [older, newer],
        {
            "room-old": [
                _participant("agent-old", kind=livekit_api.ParticipantInfo.Kind.AGENT),
                _participant("user-old", kind=livekit_api.ParticipantInfo.Kind.STANDARD),
            ],
            "room-new": [
                _participant("agent-new", kind=livekit_api.ParticipantInfo.Kind.AGENT),
                _participant("user-new", kind=livekit_api.ParticipantInfo.Kind.STANDARD),
            ],
        },
    )

    caplog.set_level("INFO", logger="openbase_coder_cli.livekit_announcer")
    result = asyncio.run(publish_announcer_message("hello", livekit_client=client))

    assert result.room_name == "room-new"
    assert result.agent_identities == ("agent-new",)
    sent = client.room.sent[0]
    assert sent.room == "room-new"
    assert sent.topic == ANNOUNCER_TOPIC
    assert sent.destination_identities == ["agent-new"]
    assert json.loads(sent.data.decode("utf-8"))["text"] == "hello"
    messages = [record.getMessage() for record in caplog.records]
    assert any("stage=announcer_publish_request" in message for message in messages)
    assert any("stage=announcer_target_resolved" in message for message in messages)
    assert any("stage=announcer_send_data_end" in message for message in messages)


def test_publish_announcer_message_uses_active_target_voice(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    (tmp_path / "livekit-voice-route.json").write_text(
        json.dumps(
            {
                "dispatcher_thread_id": "dispatcher-1",
                "active_target_thread_id": "target-1",
                "active_target_kind": "codex_thread",
                "active_target_label": "Target",
                "active_target_voice_id": "voice-1",
                "updated_at": 1,
            }
        ),
        encoding="utf-8",
    )
    client = FakeLiveKitClient(
        [_room("room-1", 100)],
        {
            "room-1": [
                _participant("agent-1", kind=livekit_api.ParticipantInfo.Kind.AGENT),
                _participant("user-1", kind=livekit_api.ParticipantInfo.Kind.STANDARD),
            ],
        },
    )

    asyncio.run(publish_announcer_message("hello", livekit_client=client))

    payload = json.loads(client.room.sent[0].data.decode("utf-8"))
    assert payload["voice_id"] == "voice-1"


def test_publish_announcer_message_prefers_explicit_voice_over_active_target(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    (tmp_path / "livekit-voice-route.json").write_text(
        json.dumps(
            {
                "dispatcher_thread_id": "dispatcher-1",
                "active_target_thread_id": "target-1",
                "active_target_kind": "codex_thread",
                "active_target_label": "Target",
                "active_target_voice_id": "route-voice",
                "updated_at": 1,
            }
        ),
        encoding="utf-8",
    )
    client = FakeLiveKitClient(
        [_room("room-1", 100)],
        {
            "room-1": [
                _participant("agent-1", kind=livekit_api.ParticipantInfo.Kind.AGENT),
                _participant("user-1", kind=livekit_api.ParticipantInfo.Kind.STANDARD),
            ],
        },
    )

    asyncio.run(
        publish_announcer_message(
            "hello",
            voice_id="super-agent-voice",
            livekit_client=client,
        )
    )

    payload = json.loads(client.room.sent[0].data.decode("utf-8"))
    assert payload["voice_id"] == "super-agent-voice"


def test_publish_announcer_message_replaces_non_english_kokoro_voice(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        dispatcher_config,
        "selected_tts_provider_id",
        lambda: KOKORO_PROVIDER_ID,
    )
    client = FakeLiveKitClient(
        [_room("room-1", 100)],
        {
            "room-1": [
                _participant("agent-1", kind=livekit_api.ParticipantInfo.Kind.AGENT),
                _participant("user-1", kind=livekit_api.ParticipantInfo.Kind.STANDARD),
            ],
        },
    )

    asyncio.run(
        publish_announcer_message(
            "hello",
            voice_id="jf_tebukuro",
            livekit_client=client,
        )
    )

    payload = json.loads(client.room.sent[0].data.decode("utf-8"))
    assert payload["voice_id"] == "af_bella"


def test_publish_announcer_message_explicit_voice_preserves_room_targeting(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    client = FakeLiveKitClient(
        [_room("room-old", 100), _room("room-explicit", 50)],
        {
            "room-old": [
                _participant("agent-old", kind=livekit_api.ParticipantInfo.Kind.AGENT),
                _participant("user-old", kind=livekit_api.ParticipantInfo.Kind.STANDARD),
            ],
            "room-explicit": [
                _participant("agent-explicit", kind=livekit_api.ParticipantInfo.Kind.AGENT),
            ],
        },
    )

    result = asyncio.run(
        publish_announcer_message(
            "hello",
            room_name="room-explicit",
            voice_id="super-agent-voice",
            livekit_client=client,
        )
    )

    sent = client.room.sent[0]
    payload = json.loads(sent.data.decode("utf-8"))
    assert result.room_name == "room-explicit"
    assert result.agent_identities == ("agent-explicit",)
    assert sent.room == "room-explicit"
    assert sent.destination_identities == ["agent-explicit"]
    assert payload["voice_id"] == "super-agent-voice"


def test_publish_announcer_audio_file_sends_path_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    audio_path = tmp_path / "done.wav"
    audio_path.write_bytes(b"audio")
    client = FakeLiveKitClient(
        [_room("room-1", 100)],
        {
            "room-1": [
                _participant("agent-1", kind=livekit_api.ParticipantInfo.Kind.AGENT),
                _participant("user-1", kind=livekit_api.ParticipantInfo.Kind.STANDARD),
            ],
        },
    )

    result = asyncio.run(
        publish_announcer_audio_file(str(audio_path), livekit_client=client)
    )

    sent = client.room.sent[0]
    payload = json.loads(sent.data.decode("utf-8"))
    assert result.room_name == "room-1"
    assert sent.topic == ANNOUNCER_TOPIC
    assert payload["kind"] == AUDIO_PLAYBACK_KIND
    assert payload["audio_path"] == str(audio_path)
    assert "text" not in payload


def test_publish_announcer_message_raises_when_no_active_room(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    client = FakeLiveKitClient(
        [_room("room-empty", 100, participants=0)],
        {"room-empty": []},
    )

    try:
        asyncio.run(publish_announcer_message("hello", livekit_client=client))
    except NoActiveLiveKitRoomError as exc:
        assert "No active LiveKit voice room" in str(exc)
    else:
        raise AssertionError("Expected NoActiveLiveKitRoomError")


def test_user_say_api_returns_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    record_voice_assignment(
        thread_id="thread-1",
        agent_name="Dottie",
        cwd="/tmp/project",
        voice_id="voice-dottie",
        voice_name="Dottie",
        kind="codex_thread",
        source="test",
    )

    async def fake_publish(text, *, room_name=None, voice_id=None):
        assert text == "hello"
        assert room_name is None
        assert voice_id == "voice-dottie"
        return SimpleNamespace(
            message_id="announcer-1",
            room_name="room-1",
        )

    monkeypatch.setattr(views, "publish_announcer_message", fake_publish)

    request = APIRequestFactory().post(
        "/api/user/say/",
        {"agent_name": "Dottie", "text": "hello"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.user_say(request)

    assert response.status_code == 202
    assert response.data == {
        "message_id": "announcer-1",
        "room_name": "room-1",
        "status": "published",
    }


def test_user_say_api_allows_authenticated_local_post_without_csrf_token(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    record_voice_assignment(
        thread_id="thread-1",
        agent_name="Dottie",
        cwd="/tmp/project",
        voice_id="voice-dottie",
        voice_name="Dottie",
        kind="codex_thread",
        source="test",
    )

    async def fake_publish(text, *, room_name=None, voice_id=None):
        assert text == "hello"
        assert room_name is None
        assert voice_id == "voice-dottie"
        return SimpleNamespace(
            message_id="announcer-1",
            room_name="room-1",
        )

    def fake_authenticate(self, request):
        return (SimpleNamespace(is_authenticated=True), {"sub": "user-1"})

    monkeypatch.setattr(views, "publish_announcer_message", fake_publish)
    monkeypatch.setattr(
        "openbase_coder_cli.config.authentication.JWTAuthentication.authenticate",
        fake_authenticate,
    )

    client = Client(enforce_csrf_checks=True)
    response = client.post(
        "/api/user/say/",
        data=json.dumps({"agent_name": "Dottie", "text": "hello"}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer jwt.token.value",
        HTTP_HOST="localhost",
    )

    assert response.status_code == 202
    assert response.json() == {
        "message_id": "announcer-1",
        "room_name": "room-1",
        "status": "published",
    }


def test_user_say_api_resolves_agent_voice(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    record_voice_assignment(
        thread_id="thread-1",
        agent_name="Dottie",
        cwd="/tmp/project",
        voice_id="super-agent-voice",
        voice_name="Dottie",
        kind="codex_thread",
        source="test",
    )

    async def fake_publish(text, *, room_name=None, voice_id=None):
        assert text == "hello"
        assert room_name == "room-1"
        assert voice_id == "super-agent-voice"
        return SimpleNamespace(
            message_id="announcer-1",
            room_name="room-1",
        )

    monkeypatch.setattr(views, "publish_announcer_message", fake_publish)

    request = APIRequestFactory().post(
        "/api/user/say/",
        {"agent_name": "dottie", "text": "hello", "room_name": "room-1"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.user_say(request)

    assert response.status_code == 202


def test_user_say_api_backfills_agent_voice_from_super_agents_state(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path / "openbase"))
    state_path = tmp_path / "super-agents-state.json"
    monkeypatch.setenv("SUPER_AGENTS_STATE_FILE", str(state_path))
    state_path.write_text(
        json.dumps(
            {
                "sessions": {
                    "thread-evie": {
                        "threadId": "thread-evie",
                        "label": "desktop-lorem-folder-file",
                        "agentName": "Evie",
                        "cwd": "/tmp/project",
                        "lastStatus": "completed",
                        "updatedAt": "2026-05-28T04:24:45.627Z",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    caplog.set_level(logging.INFO)

    async def fake_publish(text, *, room_name=None, voice_id=None):
        assert text == "hello"
        assert room_name is None
        assert voice_id
        return SimpleNamespace(
            message_id="announcer-1",
            room_name="room-1",
        )

    monkeypatch.setattr(views, "publish_announcer_message", fake_publish)

    request = APIRequestFactory().post(
        "/api/user/say/",
        {"agent_name": "evie", "text": "hello"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.user_say(request)

    assert response.status_code == 202
    messages = "\n".join(record.message for record in caplog.records)
    assert "livekit_voice_assignment_backfilled" in messages
    assert "source=super_agents_state" in messages


def test_user_say_api_backfills_agent_voice_from_claude_code_state(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path / "openbase"))
    claude_home = tmp_path / "super-agents-claude-code"
    claude_home.mkdir()
    monkeypatch.setenv("SUPER_AGENTS_CLAUDE_CODE_HOME", str(claude_home))
    with sqlite3.connect(claude_home / "state.sqlite3") as conn:
        conn.execute(
            """
            create table sessions (
                id text primary key,
                name text not null,
                agent_name text,
                cwd text not null,
                status text not null,
                created_at text not null,
                updated_at text not null
            )
            """
        )
        conn.execute(
            """
            insert into sessions (
                id, name, agent_name, cwd, status, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "s_cindy",
                "fish-markdown",
                "Cindy",
                "/tmp/fish-project",
                "waiting",
                "2026-06-20T02:58:57.899Z",
                "2026-06-20T03:00:21.134Z",
            ),
        )
    caplog.set_level(logging.INFO)

    async def fake_publish(text, *, room_name=None, voice_id=None):
        assert text == "hello"
        assert room_name is None
        assert voice_id
        return SimpleNamespace(
            message_id="announcer-1",
            room_name="room-1",
        )

    monkeypatch.setattr(views, "publish_announcer_message", fake_publish)

    request = APIRequestFactory().post(
        "/api/user/say/",
        {"agent_name": "cindy", "text": "hello"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.user_say(request)

    assert response.status_code == 202
    messages = "\n".join(record.message for record in caplog.records)
    assert "livekit_voice_assignment_backfilled" in messages
    assert "source=claude_code_state" in messages


def test_user_say_api_rejects_unknown_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))

    request = APIRequestFactory().post(
        "/api/user/say/",
        {"agent_name": "Dottie", "text": "hello"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.user_say(request)

    assert response.status_code == 404
    assert "Dottie" in response.data["detail"]


def test_user_say_api_logs_unknown_agent_lookup_diagnostics(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SUPER_AGENTS_STATE_FILE", str(tmp_path / "missing-state.json"))
    caplog.set_level(logging.WARNING)

    request = APIRequestFactory().post(
        "/api/user/say/",
        {"agent_name": "Evie", "text": "hello"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.user_say(request)

    assert response.status_code == 404
    messages = "\n".join(record.message for record in caplog.records)
    assert "stage=user_say_voice_unknown" in messages
    assert "catalog_voice_name=" in messages
    assert "normalized_agent_name': 'evie'" in messages


def test_user_say_api_selects_latest_matching_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    for thread_id, voice_id, seen_at in (
        ("thread-1", "voice-1", 10),
        ("thread-2", "voice-2", 20),
    ):
        record_voice_assignment(
            thread_id=thread_id,
            agent_name="Dottie",
            cwd="/tmp/project",
            voice_id=voice_id,
            voice_name="Dottie",
            kind="codex_thread",
            source="test",
            seen_at=seen_at,
        )

    async def fake_publish(text, *, room_name=None, voice_id=None):
        assert text == "hello"
        assert room_name is None
        assert voice_id == "voice-2"
        return SimpleNamespace(
            message_id="announcer-1",
            room_name="room-1",
        )

    monkeypatch.setattr(views, "publish_announcer_message", fake_publish)

    request = APIRequestFactory().post(
        "/api/user/say/",
        {"agent_name": "Dottie", "text": "hello"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.user_say(request)

    assert response.status_code == 202
    assert response.data["message_id"] == "announcer-1"


def test_user_say_api_returns_thread_for_no_active_room(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    record_voice_assignment(
        thread_id="thread-42",
        agent_name="Dottie",
        cwd="/tmp/project",
        voice_id="voice-dottie",
        voice_name="Dottie",
        kind="codex_thread",
        source="test",
    )

    async def no_active_room(*_args, **_kwargs):
        raise NoActiveLiveKitRoomError("No active LiveKit voice room was found.")

    monkeypatch.setattr(views, "publish_announcer_message", no_active_room)
    request = APIRequestFactory().post(
        "/api/user/say/",
        {"agent_name": "Dottie", "text": "review ready"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.user_say(request)

    assert response.status_code == 200
    assert response.data == {
        "status": "no_active_room",
        "detail": "No active LiveKit voice room was found.",
        "agent_name": "Dottie",
        "thread_id": "thread-42",
    }


def test_user_play_api_passes_audio_path(monkeypatch, tmp_path):
    audio_path = tmp_path / "done.wav"
    audio_path.write_bytes(b"audio")

    async def fake_publish(audio_file_path, *, room_name=None):
        assert audio_file_path == str(audio_path)
        assert room_name == "room-1"
        return SimpleNamespace(
            message_id="announcer-audio-1",
            room_name="room-1",
        )

    monkeypatch.setattr(views, "publish_announcer_audio_file", fake_publish)

    request = APIRequestFactory().post(
        "/api/user/play/",
        {"audio_path": str(audio_path), "room_name": "room-1"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.user_play(request)

    assert response.status_code == 202


def test_user_play_api_rejects_missing_audio_path(tmp_path):
    request = APIRequestFactory().post(
        "/api/user/play/",
        {"audio_path": str(tmp_path / "missing.wav")},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.user_play(request)

    assert response.status_code == 400


def test_livekit_companion_session_api_returns_current_room(monkeypatch):
    async def fake_resolve_companion_target_room(room_name=None):
        assert room_name is None
        return SimpleNamespace(room_name="room-1")

    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "devsecret")
    monkeypatch.setenv("LIVEKIT_CLIENT_API_KEY", "clientkey")
    monkeypatch.setenv("LIVEKIT_CLIENT_API_SECRET", "clientsecret")
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit.local")
    monkeypatch.setattr(
        views._livekit,
        "_resolve_companion_target_room",
        fake_resolve_companion_target_room,
    )

    request = APIRequestFactory().get("/api/livekit-companion-session/")
    force_authenticate(
        request,
        user=SimpleNamespace(is_authenticated=True),
        token={"email": "gabe@example.com"},
    )

    response = views.livekit_companion_session(request)

    assert response.status_code == 200
    assert response.data["roomUrl"] == "ws://livekit.local"
    assert response.data["roomName"] == "room-1"
    assert response.data["companionToken"]
    token_payload = jwt.decode(
        response.data["companionToken"],
        options={"verify_signature": False},
    )
    assert token_payload["video"]["canPublish"] is True
    assert token_payload["video"]["canSubscribe"] is False
    assert response.data["companionTokenExpiresAt"]


def test_livekit_companion_session_api_guides_missing_client_credentials(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "devsecret")
    monkeypatch.delenv("LIVEKIT_CLIENT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_CLIENT_API_SECRET", raising=False)

    request = APIRequestFactory().get("/api/livekit-companion-session/")
    force_authenticate(
        request,
        user=SimpleNamespace(is_authenticated=True),
        token={"email": "gabe@example.com"},
    )

    response = views.livekit_companion_session(request)

    assert response.status_code == 400
    assert response.data == {
        "detail": (
            "Local LiveKit client token credentials are not configured. "
            "Run 'openbase-coder setup' to generate LIVEKIT_CLIENT_API_KEY and "
            "LIVEKIT_CLIENT_API_SECRET, then restart the Openbase Coder services."
        )
    }


def test_livekit_companion_session_api_handles_missing_room(monkeypatch):
    async def fake_resolve_companion_target_room(room_name=None):
        raise NoActiveLiveKitRoomError("No active LiveKit voice room was found.")

    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "devsecret")
    monkeypatch.setenv("LIVEKIT_CLIENT_API_KEY", "clientkey")
    monkeypatch.setenv("LIVEKIT_CLIENT_API_SECRET", "clientsecret")
    monkeypatch.setattr(
        views._livekit,
        "_resolve_companion_target_room",
        fake_resolve_companion_target_room,
    )

    request = APIRequestFactory().get("/api/livekit-companion-session/")
    force_authenticate(
        request,
        user=SimpleNamespace(is_authenticated=True),
        token={"email": "gabe@example.com"},
    )

    response = views.livekit_companion_session(request)

    assert response.status_code == 404
    assert response.data == {"detail": "No active LiveKit voice room was found."}


def test_livekit_companion_start_api_starts_linux_screen_share(monkeypatch):
    class FakeCompanionClient:
        def __init__(self):
            self.calls = []

        def ensure_running(self):
            self.calls.append(("ensure_running", None))

        def start_screen_share(self, session):
            self.calls.append(("start_screen_share", session))
            return {"ok": True, "state": "sharing"}

    client = FakeCompanionClient()

    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "devsecret")
    monkeypatch.setenv("LIVEKIT_CLIENT_API_KEY", "clientkey")
    monkeypatch.setenv("LIVEKIT_CLIENT_API_SECRET", "clientsecret")
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit.local")
    monkeypatch.setattr(views._livekit.platform, "system", lambda: "Linux")
    monkeypatch.setattr(views._livekit, "_companion_client_factory", lambda: client)

    request = APIRequestFactory().post(
        "/api/livekit-companion-start/",
        {"room_name": "room-1"},
        format="json",
    )
    force_authenticate(
        request,
        user=SimpleNamespace(is_authenticated=True),
        token={"email": "gabe@example.com"},
    )

    response = views.livekit_companion_start(request)

    assert response.status_code == 200
    assert response.data["supported"] is True
    assert response.data["started"] is True
    assert response.data["roomName"] == "room-1"
    assert response.data["companion"] == {"ok": True, "state": "sharing"}
    assert [name for name, _ in client.calls] == [
        "ensure_running",
        "start_screen_share",
    ]
    assert client.calls[1][1]["roomUrl"] == "ws://livekit.local"
    assert client.calls[1][1]["token"]


def test_livekit_companion_start_api_is_noop_on_non_linux(monkeypatch):
    monkeypatch.setattr(views._livekit.platform, "system", lambda: "Darwin")

    request = APIRequestFactory().post(
        "/api/livekit-companion-start/",
        {"room_name": "room-1"},
        format="json",
    )
    force_authenticate(
        request,
        user=SimpleNamespace(is_authenticated=True),
        token={"email": "gabe@example.com"},
    )

    response = views.livekit_companion_start(request)

    assert response.status_code == 200
    assert response.data["supported"] is False
    assert response.data["started"] is False


def test_user_say_api_rejects_blank_text():
    request = APIRequestFactory().post(
        "/api/user/say/",
        {"agent_name": "Dottie", "text": ""},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.user_say(request)

    assert response.status_code == 400
