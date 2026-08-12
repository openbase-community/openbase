from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import livekit.api as livekit_api

from openbase_coder_cli import livekit_voice_route as voice_route
from openbase_coder_cli.livekit_announcer import NoActiveLiveKitRoomError
from openbase_coder_cli.livekit_voice_history import (
    get_voice_history_entry,
    record_voice_assignment,
)
from openbase_coder_cli.livekit_voice_route import (
    DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS,
    DIRECT_LIVEKIT_INSTRUCTIONS_PATH_ENV,
    DIRECT_LIVEKIT_INSTRUCTIONS_TEXT_ENV,
    VOICE_ROUTE_TOPIC,
    VoiceRouteBlockedError,
    clear_livekit_thread_state,
    get_livekit_voice_route_state,
    load_direct_livekit_developer_instructions,
    prepare_livekit_dispatcher_recreation,
    publish_exit_to_dispatch,
    publish_transfer_to_thread,
    stable_super_agent_voice_id,
    super_agent_voice_for_context,
    super_agent_voice_id_for_context,
    warm_livekit_dispatcher_thread,
)
from openbase_coder_cli.tts_providers import KOKORO_PROVIDER_ID


class FakeRoomService:
    def __init__(self):
        self.sent = []

    async def list_rooms(self, request):
        return SimpleNamespace(
            rooms=[
                SimpleNamespace(name="room-1", creation_time_ms=100, num_participants=2)
            ]
        )

    async def list_participants(self, request):
        return SimpleNamespace(
            participants=[
                SimpleNamespace(
                    identity="agent-1",
                    kind=livekit_api.ParticipantInfo.Kind.AGENT,
                    state=livekit_api.ParticipantInfo.State.ACTIVE,
                ),
                SimpleNamespace(
                    identity="user-1",
                    kind=livekit_api.ParticipantInfo.Kind.STANDARD,
                    state=livekit_api.ParticipantInfo.State.ACTIVE,
                ),
            ]
        )

    async def send_data(self, request):
        self.sent.append(request)
        return SimpleNamespace()


class FakeLiveKitClient:
    def __init__(self):
        self.room = FakeRoomService()
        self.closed = False

    async def aclose(self):
        self.closed = True


class EmptyLiveKitClient(FakeLiveKitClient):
    async def aclose(self):
        self.closed = True

    def __init__(self):
        self.room = SimpleNamespace(
            sent=[],
            list_rooms=self._list_rooms,
        )
        self.closed = False

    async def _list_rooms(self, request):
        return SimpleNamespace(rooms=[])


class FakeSessionManager:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []

    async def resume_thread_with_developer_instructions(
        self,
        thread_id: str,
        directory: str,
        developer_instructions: str,
    ) -> None:
        self.calls.append((thread_id, directory, developer_instructions))
        if self.fail:
            raise RuntimeError("resume failed")


def test_warm_livekit_dispatcher_uses_configured_super_agents_client(
    tmp_path: Path, monkeypatch
):
    calls = []
    instruction_path = tmp_path / "dispatcher.md"
    instruction_path.write_text(
        "dispatcher says random fruit is persimmon\n", encoding="utf-8"
    )

    class FakeSuperAgentsLiveKitClient:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def prepare(self):
            calls.append(("prepare", {}))
            return "dispatcher-1"

        async def aclose(self):
            calls.append(("close", {}))

    monkeypatch.setenv("LIVEKIT_CODEX_THREAD_CWD", str(tmp_path))
    monkeypatch.setenv("LIVEKIT_CODEX_THREAD_STATE_PATH", str(tmp_path / "route.json"))
    monkeypatch.setattr(
        voice_route, "CODEX_DISPATCHER_INSTRUCTIONS_PATH", instruction_path
    )

    from openbase_coder_cli.livekit_agent import super_agents_client

    monkeypatch.setattr(
        super_agents_client,
        "SuperAgentsLiveKitClient",
        FakeSuperAgentsLiveKitClient,
    )

    result = asyncio.run(warm_livekit_dispatcher_thread(timeout_seconds=0))

    assert result == "dispatcher-1"
    assert calls[0][0] == "init"
    init_kwargs = calls[0][1]
    assert init_kwargs["cwd"] == str(tmp_path)
    assert init_kwargs["state_path"] == str(tmp_path / "route.json")
    assert (
        init_kwargs["developer_instructions"]
        == "dispatcher says random fruit is persimmon"
    )
    assert init_kwargs["fresh_thread"] is False
    assert calls[1:] == [("prepare", {}), ("close", {})]

    calls.clear()
    asyncio.run(warm_livekit_dispatcher_thread(timeout_seconds=0, fresh=True))
    assert calls[0][1]["fresh_thread"] is True


def test_super_agent_voices_use_builtin_catalog_pool(monkeypatch):
    monkeypatch.setattr(
        voice_route,
        "selected_tts_provider_id",
        lambda: voice_route.CARTESIA_PROVIDER_ID,
    )
    voices = voice_route._current_super_agent_voices()

    assert len(voices) > 1
    assert voice_route.DEFAULT_CARTESIA_VOICE_ID not in {
        voice.voice_id for voice in voices
    }
    assert any(voice.name == "Katie" for voice in voices)


def test_kokoro_super_agent_voices_are_english_only(monkeypatch):
    monkeypatch.setattr(
        voice_route,
        "selected_tts_provider_id",
        lambda: KOKORO_PROVIDER_ID,
    )

    voices = voice_route._current_super_agent_voices()

    assert len(voices) > 1
    assert {voice.voice_id[:1] for voice in voices} <= {"a", "b"}
    assert "jf_tebukuro" not in {voice.voice_id for voice in voices}
    assert "zm_yunjian" not in {voice.voice_id for voice in voices}
    assert (
        super_agent_voice_id_for_context("thread-1", "Build", "Yunjian") != "zm_yunjian"
    )


def test_super_agent_voice_context_prefers_agent_name(monkeypatch):
    monkeypatch.setattr(
        voice_route,
        "selected_tts_provider_id",
        lambda: voice_route.CARTESIA_PROVIDER_ID,
    )
    monkeypatch.setattr(
        voice_route,
        "SUPER_AGENT_VOICES",
        (
            voice_route.CartesiaVoice("voice-carl", "Carl"),
            voice_route.CartesiaVoice("voice-dottie", "Dottie"),
        ),
    )
    monkeypatch.setattr(
        voice_route, "SUPER_AGENT_VOICE_IDS", ("voice-carl", "voice-dottie")
    )

    assert (
        super_agent_voice_id_for_context("thread-1", "Build", "Dottie")
        == "voice-dottie"
    )
    assert super_agent_voice_id_for_context(
        "thread-1", "Build", "Unknown"
    ) == stable_super_agent_voice_id("thread-1", "Build")
    assert (
        super_agent_voice_id_for_context("thread-1", "Build", "dottie")
        == "voice-dottie"
    )
    assert super_agent_voice_id_for_context(None, None, "Dottie") == "voice-dottie"


def test_super_agent_voice_context_can_use_catalog_name(monkeypatch):
    monkeypatch.setattr(
        voice_route,
        "selected_tts_provider_id",
        lambda: voice_route.CARTESIA_PROVIDER_ID,
    )
    assert (
        super_agent_voice_id_for_context(None, None, "Dottie")
        == "e3827ec5-697a-4b7c-9704-1a23041bbc51"
    )


def test_dispatcher_label_uses_configured_dispatcher_voice(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        voice_route,
        "dispatcher_voice",
        lambda *args, **kwargs: {
            "id": "voice-jacqueline",
            "name": "Jacqueline",
            "provider": "openbase_cloud",
        },
    )
    monkeypatch.setattr(
        voice_route,
        "selected_tts_provider_id",
        lambda: voice_route.CARTESIA_PROVIDER_ID,
    )
    monkeypatch.setattr(
        voice_route,
        "SUPER_AGENT_VOICES",
        (
            voice_route.CartesiaVoice("voice-carl", "Carl"),
            voice_route.CartesiaVoice("voice-dottie", "Dottie"),
        ),
    )
    monkeypatch.setattr(
        voice_route, "SUPER_AGENT_VOICE_IDS", ("voice-carl", "voice-dottie")
    )

    voice = super_agent_voice_for_context("dispatcher-thread-1", "dispatcher")

    assert voice == voice_route.CartesiaVoice(
        "voice-jacqueline",
        "Jacqueline",
        "openbase_cloud",
    )


def test_route_state_ignores_stale_legacy_thread_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    (tmp_path / "livekit-codex-thread.json").write_text(
        json.dumps({"thread_id": "dispatcher-1"}),
        encoding="utf-8",
    )

    state = get_livekit_voice_route_state()

    assert state.dispatcher_thread_id is None
    assert state.dispatcher_voice_name
    assert state.active_route == "dispatcher"
    assert not (tmp_path / "livekit-voice-route.json").is_file()


def test_clear_livekit_thread_state_removes_route_and_legacy_files(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    route_path = tmp_path / "livekit-voice-route.json"
    legacy_path = tmp_path / "livekit-codex-thread.json"
    route_path.write_text(
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
    legacy_path.write_text(json.dumps({"thread_id": "legacy-1"}), encoding="utf-8")

    result = clear_livekit_thread_state()

    assert result["previous_dispatcher_thread_id"] == "dispatcher-1"
    assert result["previous_active_target_thread_id"] == "target-1"
    assert str(route_path) in result["removed_paths"]
    assert str(legacy_path) in result["removed_paths"]
    assert not route_path.exists()
    assert not legacy_path.exists()


def test_prepare_livekit_dispatcher_recreation_resets_dispatcher_route(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    route_path = tmp_path / "livekit-voice-route.json"
    legacy_path = tmp_path / "livekit-codex-thread.json"
    route_path.write_text(
        json.dumps(
            {
                "dispatcher_thread_id": "dispatcher-1",
                "dispatcher_voice_id": "dispatcher-voice",
                "dispatcher_voice_name": "Dispatcher",
                "active_target_thread_id": "target-1",
                "active_target_kind": "codex_thread",
                "active_target_label": "Target",
                "active_target_voice_id": "voice-1",
                "active_target_voice_name": "Alice",
                "updated_at": 1,
            }
        ),
        encoding="utf-8",
    )
    legacy_path.write_text(json.dumps({"thread_id": "dispatcher-1"}), encoding="utf-8")

    result = prepare_livekit_dispatcher_recreation()
    state = get_livekit_voice_route_state()

    assert result["previous_dispatcher_thread_id"] == "dispatcher-1"
    assert result["previous_active_target_thread_id"] == "target-1"
    assert str(legacy_path) in result["removed_paths"]
    assert result["reset_route_path"] == str(route_path)
    assert not legacy_path.exists()
    assert route_path.exists()
    assert state.dispatcher_thread_id is None
    assert state.active_target_thread_id is None
    assert state.active_route == "dispatcher"


def test_direct_livekit_instruction_loader_priority(tmp_path: Path):
    explicit = tmp_path / "explicit.md"
    default = tmp_path / "default.md"
    explicit.write_text("explicit file instructions\n", encoding="utf-8")
    default.write_text("default file instructions\n", encoding="utf-8")

    assert (
        load_direct_livekit_developer_instructions(
            env={
                DIRECT_LIVEKIT_INSTRUCTIONS_PATH_ENV: str(explicit),
                DIRECT_LIVEKIT_INSTRUCTIONS_TEXT_ENV: "env text instructions",
            },
            default_path=default,
        )
        == "explicit file instructions"
    )

    assert (
        load_direct_livekit_developer_instructions(
            env={DIRECT_LIVEKIT_INSTRUCTIONS_TEXT_ENV: "env text instructions"},
            default_path=default,
        )
        == "default file instructions"
    )

    assert (
        load_direct_livekit_developer_instructions(
            env={DIRECT_LIVEKIT_INSTRUCTIONS_TEXT_ENV: "env text instructions"},
            default_path=tmp_path / "missing.md",
        )
        == "env text instructions"
    )

    assert (
        load_direct_livekit_developer_instructions(
            env={},
            default_path=tmp_path / "missing.md",
        )
        == DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS
    )


def test_transfer_to_thread_prepares_then_publishes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    voice_instructions_path = tmp_path / "VOICE_INSTRUCTIONS.md"
    voice_instructions_path.write_text("direct voice instructions\n", encoding="utf-8")
    monkeypatch.setattr(
        voice_route,
        "selected_tts_provider_id",
        lambda: voice_route.CARTESIA_PROVIDER_ID,
    )
    monkeypatch.setattr(
        voice_route,
        "CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH",
        voice_instructions_path,
    )
    monkeypatch.setattr(
        voice_route,
        "SUPER_AGENT_VOICES",
        (
            voice_route.CartesiaVoice("voice-a", "Alice"),
            voice_route.CartesiaVoice("voice-b", "Bob"),
            voice_route.CartesiaVoice("voice-dottie", "Dottie"),
        ),
    )
    monkeypatch.setattr(
        voice_route, "SUPER_AGENT_VOICE_IDS", ("voice-a", "voice-b", "voice-dottie")
    )
    manager = FakeSessionManager()
    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.session_manager.get_session_manager",
        lambda: manager,
    )
    client = FakeLiveKitClient()

    result = asyncio.run(
        publish_transfer_to_thread(
            "target-1",
            directory="/tmp/project",
            label="Project",
            agent_name="Dottie",
            livekit_client=client,
        )
    )

    assert result.state.active_target_thread_id == "target-1"
    assert manager.calls == [("target-1", "/tmp/project", "direct voice instructions")]
    sent = client.room.sent[0]
    payload = json.loads(sent.data.decode("utf-8"))
    assert payload["action"] == "transfer_to_thread"
    assert payload["thread_id"] == "target-1"
    assert payload["cwd"] == "/tmp/project"
    assert payload["agent_name"] == "Dottie"
    assert "voice_id" not in payload
    assert payload["state"]["active_target_voice_id"] == "voice-dottie"
    assert (
        payload["state"]["active_target_voice_name"]
        == result.state.active_target_voice_name
    )
    assert payload["state"]["dispatcher_voice_name"]
    state = get_livekit_voice_route_state()
    assert state.active_target_thread_id == "target-1"
    assert state.active_target_voice_id == "voice-dottie"
    assert state.active_target_voice_name == "Dottie"
    history = get_voice_history_entry("target-1")
    assert history is not None
    assert history.agent_name == "Dottie"
    assert history.voice_id == state.active_target_voice_id
    assert history.source == "route_transfer"


def test_transfer_to_thread_rejects_dispatcher_target(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    (tmp_path / "livekit-voice-route.json").write_text(
        json.dumps(
            {
                "dispatcher_thread_id": "dispatcher-1",
                "dispatcher_voice_id": "voice-jacqueline",
                "dispatcher_voice_name": "Jacqueline",
                "updated_at": 1,
                "instruction_override_supported": True,
            }
        ),
        encoding="utf-8",
    )
    manager = FakeSessionManager()
    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.session_manager.get_session_manager",
        lambda: manager,
    )
    client = FakeLiveKitClient()

    async def check():
        try:
            await publish_transfer_to_thread(
                "dispatcher-1",
                directory="/tmp/project",
                label="dispatcher",
                livekit_client=client,
            )
        except VoiceRouteBlockedError as exc:
            assert "dispatcher thread cannot be used" in str(exc)
        else:
            raise AssertionError("Expected VoiceRouteBlockedError")

    asyncio.run(check())
    assert manager.calls == []
    assert client.room.sent == []


def test_transfer_to_thread_reuses_existing_thread_voice_history(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        voice_route,
        "selected_tts_provider_id",
        lambda: voice_route.CARTESIA_PROVIDER_ID,
    )
    monkeypatch.setattr(
        voice_route,
        "SUPER_AGENT_VOICES",
        (
            voice_route.CartesiaVoice("voice-asher", "Asher"),
            voice_route.CartesiaVoice("voice-brooke", "Brooke"),
        ),
    )
    monkeypatch.setattr(
        voice_route,
        "SUPER_AGENT_VOICE_IDS",
        ("voice-asher", "voice-brooke"),
    )
    record_voice_assignment(
        thread_id="target-1",
        agent_name="Asher",
        cwd="/tmp/project",
        voice_id="voice-brooke",
        voice_name="Asher",
        kind="codex_thread",
        source="super_agents_state",
        seen_at=10,
    )
    manager = FakeSessionManager()
    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.session_manager.get_session_manager",
        lambda: manager,
    )
    client = FakeLiveKitClient()

    result = asyncio.run(
        publish_transfer_to_thread(
            "target-1",
            directory="/tmp/project",
            label="Asher",
            livekit_client=client,
        )
    )

    sent = client.room.sent[0]
    payload = json.loads(sent.data.decode("utf-8"))
    assert payload["agent_name"] == "Asher"
    assert payload["state"]["active_target_voice_id"] == "voice-asher"
    assert payload["state"]["active_target_voice_name"] == "Asher"
    assert result.state.active_target_voice_id == "voice-asher"
    history = get_voice_history_entry("target-1")
    assert history is not None
    assert history.agent_name == "Asher"
    assert history.voice_id == "voice-asher"


def test_transfer_to_thread_fails_when_prepare_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    manager = FakeSessionManager(fail=True)
    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.session_manager.get_session_manager",
        lambda: manager,
    )
    client = FakeLiveKitClient()

    async def check():
        try:
            await publish_transfer_to_thread(
                "target-1",
                directory="/tmp/project",
                livekit_client=client,
            )
        except RuntimeError as exc:
            assert "resume failed" in str(exc)
        else:
            raise AssertionError("Expected RuntimeError")

    asyncio.run(check())
    assert client.room.sent == []
    assert get_livekit_voice_route_state().active_target_thread_id is None


def test_exit_to_dispatch_publishes_route_packet(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    client = FakeLiveKitClient()

    result = asyncio.run(publish_exit_to_dispatch(livekit_client=client))

    assert result.room_name == "room-1"
    sent = client.room.sent[0]
    assert sent.topic == VOICE_ROUTE_TOPIC
    assert sent.destination_identities == ["agent-1"]
    payload = json.loads(sent.data.decode("utf-8"))
    assert payload["action"] == "exit_to_dispatch"
    assert payload["state"]["active_target_thread_id"] is None


def test_exit_to_dispatch_does_not_persist_when_no_room(tmp_path: Path, monkeypatch):
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

    async def check():
        try:
            await publish_exit_to_dispatch(livekit_client=EmptyLiveKitClient())
        except NoActiveLiveKitRoomError:
            pass
        else:
            raise AssertionError("Expected NoActiveLiveKitRoomError")

    asyncio.run(check())
    state = get_livekit_voice_route_state()
    assert state.active_target_thread_id == "target-1"
