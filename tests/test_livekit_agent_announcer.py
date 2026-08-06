from __future__ import annotations

import asyncio
import json
import logging
import wave
from types import SimpleNamespace

import pytest
from livekit import rtc

from openbase_coder_cli import livekit_voice_route as voice_route
from openbase_coder_cli.livekit_agent import audio_scoring, config, livekit, voices
from openbase_coder_cli.livekit_agent.codex_app_client import CodexAppServerClient
from openbase_coder_cli.livekit_agent.livekit import (
    ANNOUNCER_AUDIO_KIND,
    ANNOUNCER_TOPIC,
    DEFAULT_CARTESIA_TTS_VOLUME,
    DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS,
    DIRECT_LIVEKIT_INSTRUCTIONS_PATH_ENV,
    DIRECT_LIVEKIT_INSTRUCTIONS_TEXT_ENV,
    EXIT_TO_DISPATCH_PHRASE,
    VOICE_ROUTE_TOPIC,
    AnnouncerAudioMessage,
    AnnouncerMessage,
    AnnouncerSpeechQueue,
    BrainScoreAudioScorer,
    LiveKitVoiceRouter,
    VoiceRouteCommand,
    VoiceSelectingCartesiaTTS,
    _is_exit_to_dispatch_command,
    _normalize_spoken_command,
    cartesia,
    load_direct_livekit_developer_instructions,
    parse_announcer_audio_packet,
    parse_announcer_packet,
    parse_voice_route_packet,
    stable_super_agent_voice_id,
)
from openbase_coder_cli.livekit_agent.packets import VOICE_LIFECYCLE_PROTOCOL_VERSION
from openbase_coder_cli.livekit_agent.super_agents_client import (
    SuperAgentsLiveKitClient,
)
from openbase_coder_cli.livekit_agent.turn_detection import UserTurnClosureDecision
from openbase_coder_cli.livekit_agent.voice_delivery import VoiceRouteSnapshot
from openbase_coder_cli.tts_providers import KOKORO_PROVIDER_ID


def test_parse_announcer_packet_ignores_other_topics():
    packet = rtc.DataPacket(
        data=b'{"message_id":"1","text":"hello"}',
        kind=rtc.DataPacketKind.Value("KIND_RELIABLE"),
        participant=None,
        topic="lk.chat",
    )

    assert parse_announcer_packet(packet) is None


def test_parse_announcer_packet_reads_message():
    packet = rtc.DataPacket(
        data=b'{"message_id":"announcer-1","text":" hello ","voice_id":"voice-1"}',
        kind=rtc.DataPacketKind.Value("KIND_RELIABLE"),
        participant=None,
        topic=ANNOUNCER_TOPIC,
    )

    assert parse_announcer_packet(packet) == AnnouncerMessage(
        message_id="announcer-1",
        text="hello",
        voice_id="voice-1",
    )


def test_parse_announcer_audio_packet_reads_path():
    packet = rtc.DataPacket(
        data=(
            f'{{"kind":"{ANNOUNCER_AUDIO_KIND}",'
            '"message_id":"audio-1","audio_path":"/tmp/done.wav"}'
        ).encode("utf-8"),
        kind=rtc.DataPacketKind.Value("KIND_RELIABLE"),
        participant=None,
        topic=ANNOUNCER_TOPIC,
    )

    assert parse_announcer_audio_packet(packet) == AnnouncerAudioMessage(
        message_id="audio-1",
        audio_path="/tmp/done.wav",
    )
    assert parse_announcer_packet(packet) is None


def test_openbase_cloud_audio_token_fails_closed_when_login_missing(monkeypatch):
    class MissingMachineTokenManager:
        def __init__(self, web_backend_url):
            self.web_backend_url = web_backend_url

        def get_machine_token(self):
            raise livekit.AuthLoginRequiredError("login required")

    monkeypatch.setattr(livekit, "WEB_BACKEND_URL", "https://app.openbase.cloud")
    monkeypatch.setattr(livekit, "MachineTokenManager", MissingMachineTokenManager)

    with pytest.raises(livekit.OpenbaseCloudAudioAuthenticationError) as exc_info:
        livekit._openbase_cloud_audio_token()

    message = str(exc_info.value)
    assert "Openbase Cloud audio is selected" in message
    assert "openbase-coder login" in message
    assert "direct provider keys or local audio" in message


def test_openbase_cloud_audio_token_fails_closed_on_empty_token(monkeypatch):
    class EmptyMachineTokenManager:
        def __init__(self, web_backend_url):
            self.web_backend_url = web_backend_url

        def get_machine_token(self):
            return ""

    monkeypatch.setattr(livekit, "WEB_BACKEND_URL", "https://app.openbase.cloud")
    monkeypatch.setattr(livekit, "MachineTokenManager", EmptyMachineTokenManager)

    with pytest.raises(livekit.OpenbaseCloudAudioAuthenticationError) as exc_info:
        livekit._openbase_cloud_audio_token()

    assert "empty Openbase machine token" in str(exc_info.value)


def test_livekit_agent_capacity_uses_livekit_defaults_for_remote_models(monkeypatch):
    monkeypatch.delenv(livekit.LIVEKIT_AGENT_LOAD_THRESHOLD_ENV, raising=False)
    monkeypatch.delenv(livekit.LIVEKIT_AGENT_NUM_IDLE_PROCESSES_ENV, raising=False)
    monkeypatch.setattr(
        livekit,
        "selected_stt_provider_id",
        lambda: livekit.ASSEMBLYAI_STT_PROVIDER_ID,
    )
    monkeypatch.setattr(
        livekit,
        "selected_tts_provider_id",
        lambda: livekit.CARTESIA_PROVIDER_ID,
    )

    assert livekit._livekit_agent_server_options() == {}


def test_livekit_agent_capacity_uses_local_friendly_defaults_for_local_stt(monkeypatch):
    monkeypatch.delenv(livekit.LIVEKIT_AGENT_LOAD_THRESHOLD_ENV, raising=False)
    monkeypatch.delenv(livekit.LIVEKIT_AGENT_NUM_IDLE_PROCESSES_ENV, raising=False)
    monkeypatch.setattr(
        livekit,
        "selected_stt_provider_id",
        lambda: livekit.LOCAL_MLX_WHISPER_STT_PROVIDER_ID,
    )
    monkeypatch.setattr(
        livekit,
        "selected_tts_provider_id",
        lambda: livekit.CARTESIA_PROVIDER_ID,
    )

    assert livekit._livekit_agent_server_options() == {
        "load_threshold": float("inf"),
        "num_idle_processes": 1,
    }


def test_livekit_agent_capacity_uses_local_friendly_defaults_for_local_tts(monkeypatch):
    monkeypatch.delenv(livekit.LIVEKIT_AGENT_LOAD_THRESHOLD_ENV, raising=False)
    monkeypatch.delenv(livekit.LIVEKIT_AGENT_NUM_IDLE_PROCESSES_ENV, raising=False)
    monkeypatch.setattr(
        livekit,
        "selected_stt_provider_id",
        lambda: livekit.ASSEMBLYAI_STT_PROVIDER_ID,
    )
    monkeypatch.setattr(
        livekit,
        "selected_tts_provider_id",
        lambda: KOKORO_PROVIDER_ID,
    )

    assert livekit._livekit_agent_server_options() == {
        "load_threshold": float("inf"),
        "num_idle_processes": 1,
    }


def test_livekit_agent_capacity_accepts_env_overrides_for_remote_models(monkeypatch):
    monkeypatch.setenv(livekit.LIVEKIT_AGENT_LOAD_THRESHOLD_ENV, "1.5")
    monkeypatch.setenv(livekit.LIVEKIT_AGENT_NUM_IDLE_PROCESSES_ENV, "3")
    monkeypatch.setattr(
        livekit,
        "selected_stt_provider_id",
        lambda: livekit.ASSEMBLYAI_STT_PROVIDER_ID,
    )
    monkeypatch.setattr(
        livekit,
        "selected_tts_provider_id",
        lambda: livekit.CARTESIA_PROVIDER_ID,
    )

    assert livekit._livekit_agent_server_options() == {
        "load_threshold": 1.5,
        "num_idle_processes": 3,
    }


def test_livekit_agent_capacity_ignores_invalid_env_for_remote_models(monkeypatch):
    monkeypatch.setenv(livekit.LIVEKIT_AGENT_LOAD_THRESHOLD_ENV, "nope")
    monkeypatch.setenv(livekit.LIVEKIT_AGENT_NUM_IDLE_PROCESSES_ENV, "-1")
    monkeypatch.setattr(
        livekit,
        "selected_stt_provider_id",
        lambda: livekit.ASSEMBLYAI_STT_PROVIDER_ID,
    )
    monkeypatch.setattr(
        livekit,
        "selected_tts_provider_id",
        lambda: livekit.CARTESIA_PROVIDER_ID,
    )

    assert livekit._livekit_agent_server_options() == {}


def test_parse_voice_route_packet_reads_exit_action():
    packet = rtc.DataPacket(
        data=b'{"action":"exit_to_dispatch"}',
        kind=rtc.DataPacketKind.Value("KIND_RELIABLE"),
        participant=None,
        topic=VOICE_ROUTE_TOPIC,
    )

    assert parse_voice_route_packet(packet) == VoiceRouteCommand(
        action="exit_to_dispatch"
    )


def test_parse_voice_route_packet_reads_transfer_fields():
    packet = rtc.DataPacket(
        data=b'{"action":"transfer_to_thread","thread_id":"thr-1","cwd":"/tmp/project","label":"Project","state":{"active_target_voice_id":"voice-1","active_target_voice_name":"Alice"}}',
        kind=rtc.DataPacketKind.Value("KIND_RELIABLE"),
        participant=None,
        topic=VOICE_ROUTE_TOPIC,
    )

    assert parse_voice_route_packet(packet) == VoiceRouteCommand(
        action="transfer_to_thread",
        thread_id="thr-1",
        cwd="/tmp/project",
        label="Project",
        active_target_voice_id="voice-1",
        active_target_voice_name="Alice",
    )


def test_parse_voice_route_packet_uses_agent_name_as_voice_name_fallback():
    packet = rtc.DataPacket(
        data=b'{"action":"transfer_to_thread","thread_id":"thr-1","cwd":"/tmp/project","label":"Project","agent_name":"Dottie","state":{"active_target_voice_id":"voice-1"}}',
        kind=rtc.DataPacketKind.Value("KIND_RELIABLE"),
        participant=None,
        topic=VOICE_ROUTE_TOPIC,
    )

    assert parse_voice_route_packet(packet) == VoiceRouteCommand(
        action="transfer_to_thread",
        thread_id="thr-1",
        cwd="/tmp/project",
        label="Project",
        active_target_voice_id="voice-1",
        active_target_voice_name="Dottie",
    )


def test_exit_to_dispatch_phrase_normalizes_exactly():
    assert _normalize_spoken_command("Exit to dispatch.") == EXIT_TO_DISPATCH_PHRASE
    assert (
        _normalize_spoken_command("Exit,\n  to   dispatch.") == EXIT_TO_DISPATCH_PHRASE
    )


@pytest.mark.asyncio
async def test_brain_score_audio_scorer_uploads_every_interval(tmp_path, monkeypatch):
    uploads = []

    async def fake_upload(**kwargs):
        with wave.open(str(kwargs["wav_path"]), "rb") as wav_file:
            uploads.append(
                {
                    "chunk_index": kwargs["chunk_index"],
                    "duration_seconds": kwargs["duration_seconds"],
                    "sample_rate": wav_file.getframerate(),
                    "num_channels": wav_file.getnchannels(),
                    "frames": wav_file.getnframes(),
                    "token": kwargs["token"],
                }
            )

    monkeypatch.setattr(audio_scoring, "BRAIN_SCORE_ENABLED", True)
    monkeypatch.setattr(audio_scoring, "_load_brain_score_token", lambda: "token-1")
    monkeypatch.setattr(audio_scoring, "_upload_brain_score_chunk", fake_upload)

    scorer = BrainScoreAudioScorer(
        interval_seconds=0.02,
        min_duration_seconds=0.02,
        output_path=tmp_path / "brain_score.json",
        endpoint="http://example.invalid/score",
    )
    scorer.push_frame(rtc.AudioFrame.create(48000, 1, 480))
    assert uploads == []

    scorer.push_frame(rtc.AudioFrame.create(48000, 1, 480))
    await scorer.aclose()

    assert uploads == [
        {
            "chunk_index": 1,
            "duration_seconds": 0.02,
            "sample_rate": 48000,
            "num_channels": 1,
            "frames": 960,
            "token": "token-1",
        }
    ]


@pytest.mark.asyncio
async def test_brain_score_audio_scorer_skips_chunks_below_min_duration(
    tmp_path, monkeypatch
):
    uploads = []

    async def fake_upload(**kwargs):
        uploads.append(kwargs)

    monkeypatch.setattr(audio_scoring, "BRAIN_SCORE_ENABLED", True)
    monkeypatch.setattr(audio_scoring, "_load_brain_score_token", lambda: "token-1")
    monkeypatch.setattr(audio_scoring, "_upload_brain_score_chunk", fake_upload)

    scorer = BrainScoreAudioScorer(
        interval_seconds=0.02,
        min_duration_seconds=0.03,
        output_path=tmp_path / "brain_score.json",
        endpoint="http://example.invalid/score",
    )

    scorer.push_frame(rtc.AudioFrame.create(48000, 1, 480))
    scorer.push_frame(rtc.AudioFrame.create(48000, 1, 480))
    await scorer.aclose()

    assert uploads == []


@pytest.mark.asyncio
async def test_brain_score_audio_scorer_skips_chunks_during_cooldown(
    tmp_path, monkeypatch
):
    uploads = []
    score_path = tmp_path / "brain_score.json"
    score_path.write_text(json.dumps({"updated_at": 1000.0}), encoding="utf-8")

    async def fake_upload(**kwargs):
        uploads.append(kwargs)

    monkeypatch.setattr(audio_scoring, "BRAIN_SCORE_ENABLED", True)
    monkeypatch.setattr(audio_scoring, "_load_brain_score_token", lambda: "token-1")
    monkeypatch.setattr(audio_scoring, "_upload_brain_score_chunk", fake_upload)
    monkeypatch.setattr(audio_scoring.time, "time", lambda: 1100.0)

    scorer = BrainScoreAudioScorer(
        interval_seconds=0.02,
        min_duration_seconds=0.02,
        cooldown_seconds=1800,
        output_path=score_path,
        endpoint="http://example.invalid/score",
    )

    scorer.push_frame(rtc.AudioFrame.create(48000, 1, 480))
    scorer.push_frame(rtc.AudioFrame.create(48000, 1, 480))
    await scorer.aclose()

    assert uploads == []


@pytest.mark.asyncio
async def test_brain_score_audio_scorer_logs_schedule_failure_without_raising(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(audio_scoring, "BRAIN_SCORE_ENABLED", True)
    scorer = BrainScoreAudioScorer(
        interval_seconds=0.01,
        min_duration_seconds=0,
        cooldown_seconds=0,
        output_path=tmp_path / "brain_score.json",
        endpoint="http://example.invalid/score",
    )

    def fail_schedule(*, reason):
        raise RuntimeError(f"schedule failed: {reason}")

    monkeypatch.setattr(scorer, "_schedule_current_chunk", fail_schedule)
    caplog.set_level(
        logging.WARNING, logger="openbase_coder_cli.livekit_agent.audio_scoring"
    )

    scorer.push_frame(rtc.AudioFrame.create(48000, 1, 480))

    assert any(
        "brain_score stage=schedule_failed" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_upload_brain_score_chunk_logs_missing_score_without_writing(
    tmp_path, monkeypatch, caplog
):
    wav_path = tmp_path / "chunk.wav"
    wav_path.write_bytes(b"not-a-real-wav")
    writes = []

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, exc_tb):
            return None

        async def text(self):
            return json.dumps(
                {"statusCode": 200, "message": "No score yet", "data": {}}
            )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, exc_tb):
            return None

        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(audio_scoring.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(
        audio_scoring,
        "_write_brain_score_json",
        lambda path, payload: writes.append((path, payload)),
    )
    caplog.set_level(
        logging.WARNING, logger="openbase_coder_cli.livekit_agent.audio_scoring"
    )

    await audio_scoring._upload_brain_score_chunk(
        wav_path=wav_path,
        token="token-1",
        endpoint="http://example.invalid/score",
        output_path=tmp_path / "brain_score.json",
        chunk_index=1,
        duration_seconds=20,
        sample_rate=24000,
        num_channels=1,
        reason="interval",
    )

    assert writes == []
    assert not wav_path.exists()
    assert any(
        "brain_score stage=score_failed" in record.message for record in caplog.records
    )


@pytest.mark.parametrize(
    "spoken",
    [
        "Exit to dispatch.",
        "Please exit to dispatch now.",
        "Please exit,\n  to   dispatch now.",
        "To dispatch.",
        "Send me to dispatch, please.",
        "Two dispatch.",
        "Take me two dispatch.",
    ],
)
def test_exit_to_dispatch_command_accepts_short_variants(spoken):
    assert _is_exit_to_dispatch_command(spoken)


def test_exit_to_dispatch_command_rejects_embedded_variants():
    assert not _is_exit_to_dispatch_command("dispatch")
    assert not _is_exit_to_dispatch_command("please dispatch me")


@pytest.mark.parametrize(
    "spoken",
    [
        "Why do you keep saying back to dispatch every time I talk?",
        "Point my recent question to dispatch and make sure it gets answered.",
        "I want you to dispatch a new agent to review the PR.",
        "Can you send my question to dispatch for me please?",
    ],
)
def test_exit_to_dispatch_command_rejects_sentences_mentioning_dispatch(spoken):
    assert not _is_exit_to_dispatch_command(spoken)


def test_direct_livekit_instruction_loader_priority(tmp_path):
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


def test_super_agent_voices_use_builtin_catalog_pool(monkeypatch):
    monkeypatch.setattr(
        voice_route,
        "selected_tts_provider_id",
        lambda: livekit.CARTESIA_PROVIDER_ID,
    )
    voice_pool = livekit._current_super_agent_voices()

    assert len(voice_pool) > 1
    assert livekit.DEFAULT_CARTESIA_VOICE_ID not in {
        voice.voice_id for voice in voice_pool
    }
    assert any(voice.name == "Katie" for voice in voice_pool)


def test_kokoro_super_agent_voices_are_english_only(monkeypatch):
    monkeypatch.setattr(
        voice_route,
        "selected_tts_provider_id",
        lambda: KOKORO_PROVIDER_ID,
    )

    voice_pool = livekit._current_super_agent_voices()

    assert len(voice_pool) > 1
    assert {voice.voice_id[:1] for voice in voice_pool} <= {"a", "b"}
    assert "jf_tebukuro" not in {voice.voice_id for voice in voice_pool}
    assert "zm_yunjian" not in {voice.voice_id for voice in voice_pool}


class FakeSpeechHandle:
    def __init__(self, *, done: bool = False) -> None:
        self._done = done
        self.waited = False

    def done(self) -> bool:
        return self._done

    async def wait_for_playout(self) -> None:
        self.waited = True
        self._done = True


class FakeTTS:
    def __init__(self) -> None:
        self.closed = False
        self.synthesized_texts = []

    def synthesize(self, text: str):
        self.synthesized_texts.append(text)
        return FakeTTSStream()

    def synthesize_with_voice(self, text: str, *, voice_id: str | None):
        self.synthesized_texts.append(text)
        return FakeTTSStream()

    def resolve_voice_id(self, voice_id: str | None) -> str:
        return voice_id or "announcer-default-voice"

    def resolve_voice_name(self, voice_id: str | None) -> str | None:
        return "Requested Voice" if voice_id else "Announcer"

    async def aclose(self) -> None:
        self.closed = True


class FakeTTSStream:
    def __init__(self) -> None:
        self.pushed_texts = []
        self.flushed = False
        self.ended = False
        self.closed = False

    def push_text(self, token: str) -> None:
        self.pushed_texts.append(token)

    def flush(self) -> None:
        self.flushed = True

    def end_input(self) -> None:
        self.ended = True

    async def aclose(self) -> None:
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class RecordingCartesiaTTS:
    created = []

    def __init__(self, *, model, voice, api_key, volume):
        self.model = model
        self.provider = "Cartesia"
        self.capabilities = object()
        self.sample_rate = 24000
        self.num_channels = 1
        self.voice = voice
        self.api_key = api_key
        self.volume = volume
        self.prewarmed = False
        self.closed = False
        self.stream_calls = 0
        self.synthesize_calls = []
        self.stream_instance = None
        RecordingCartesiaTTS.created.append(self)

    def synthesize(self, text: str, *, conn_options=None):
        self.synthesize_calls.append(text)
        return FakeTTSStream()

    def stream(self, *, conn_options=None):
        self.stream_calls += 1
        self.stream_instance = FakeTTSStream()
        return self.stream_instance

    def prewarm(self) -> None:
        self.prewarmed = True

    async def aclose(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self) -> None:
        self.current = FakeSpeechHandle(done=False)
        self.say_calls = []
        self.say_handle = FakeSpeechHandle(done=False)
        self.user_state = "listening"
        self.agent_state = "idle"

    @property
    def current_speech(self):
        return self.current

    def say(self, text, **kwargs):
        self.say_calls.append((text, kwargs))
        return self.say_handle


def test_voice_selecting_tts_delegates_stream_to_active_voice(monkeypatch):
    RecordingCartesiaTTS.created = []
    active_voice_id = "voice-2"
    monkeypatch.setattr(cartesia, "TTS", RecordingCartesiaTTS)
    tts = VoiceSelectingCartesiaTTS(
        default_voice_id="voice-1",
        active_voice_id=lambda: active_voice_id,
        api_key="key",
    )

    stream = tts.stream()
    tts.prewarm()

    stream.push_text("- Update README.md\n- Run uv")
    stream.flush()

    assert [created.voice for created in RecordingCartesiaTTS.created] == [
        "voice-1",
        "voice-2",
    ]
    assert [created.volume for created in RecordingCartesiaTTS.created] == [
        DEFAULT_CARTESIA_TTS_VOLUME,
        DEFAULT_CARTESIA_TTS_VOLUME,
    ]
    assert RecordingCartesiaTTS.created[1].stream_calls == 1
    assert RecordingCartesiaTTS.created[1].prewarmed is True
    assert RecordingCartesiaTTS.created[1].stream_instance.pushed_texts == [
        "Update read me dot M D. Run U V."
    ]


def test_voice_selecting_tts_formats_synthesize_text(monkeypatch, caplog):
    RecordingCartesiaTTS.created = []
    monkeypatch.setattr(cartesia, "TTS", RecordingCartesiaTTS)
    tts = VoiceSelectingCartesiaTTS(
        default_voice_id="voice-1",
        default_voice_name="Jacqueline",
        active_voice_id=lambda: None,
        api_key="key",
    )

    caplog.set_level(
        logging.INFO, logger="openbase_coder_cli.livekit_agent.tts_selection"
    )
    tts.synthesize("Run `uv run pytest` and update README.md")

    assert RecordingCartesiaTTS.created[0].synthesize_calls == [
        "Run U V run pytest and update read me dot M D."
    ]
    assert any(
        "stage=tts_synthesize_start" in record.message
        and "voice_id=voice-1" in record.message
        and "voice_name=Jacqueline" in record.message
        and "Run U V run pytest" in record.message
        for record in caplog.records
    )


def test_voice_selecting_tts_logs_stream_voice_and_text(monkeypatch, caplog):
    RecordingCartesiaTTS.created = []
    monkeypatch.setattr(cartesia, "TTS", RecordingCartesiaTTS)
    tts = VoiceSelectingCartesiaTTS(
        default_voice_id="dispatcher-voice",
        default_voice_name="Jacqueline",
        active_voice_id=lambda: "agent-voice",
        active_voice_name=lambda: "Alice",
        api_key="key",
    )

    caplog.set_level(
        logging.INFO, logger="openbase_coder_cli.livekit_agent.tts_selection"
    )
    stream = tts.stream()
    stream.push_text("Yes, I'm here.")
    stream.flush()

    assert any(
        "stage=tts_stream_flush" in record.message
        and "voice_id=agent-voice" in record.message
        and "voice_name=Alice" in record.message
        and "Yes, I'm here." in record.message
        for record in caplog.records
    )


def test_voice_selecting_tts_logs_default_stream_voice_and_text(monkeypatch, caplog):
    RecordingCartesiaTTS.created = []
    monkeypatch.setattr(cartesia, "TTS", RecordingCartesiaTTS)
    tts = VoiceSelectingCartesiaTTS(
        default_voice_id="dispatcher-voice",
        default_voice_name="Jacqueline",
        active_voice_id=lambda: None,
        api_key="key",
    )

    caplog.set_level(
        logging.INFO, logger="openbase_coder_cli.livekit_agent.tts_selection"
    )
    stream = tts.stream()
    stream.push_text("Yes, I'm here.")
    stream.flush()

    assert any(
        "stage=tts_stream_flush" in record.message
        and "voice_id=dispatcher-voice" in record.message
        and "voice_name=Jacqueline" in record.message
        and "Yes, I'm here." in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_announcer_queue_waits_and_excludes_chat_context(caplog):
    session = FakeSession()
    fake_tts = FakeTTS()
    queue = AnnouncerSpeechQueue(
        session=session,
        announcer_tts=fake_tts,
        silence_grace_seconds=0,
    )

    caplog.set_level(
        logging.INFO, logger="openbase_coder_cli.livekit_agent.speech_queue"
    )
    await queue._speak(
        AnnouncerMessage(
            message_id="announcer-1",
            text="- Update README.md\n- Run uv",
            voice_id="requested-voice",
        )
    )

    assert session.current.waited is True
    assert session.say_handle.waited is True
    assert session.say_calls[0][0] == "Update read me dot M D. Run U V."
    assert session.say_calls[0][1]["allow_interruptions"] is False
    assert session.say_calls[0][1]["add_to_chat_ctx"] is False
    assert session.say_calls[0][1]["audio"] is not None
    assert any(
        "stage=announcer_say_start" in record.message
        and "voice_id=requested-voice" in record.message
        and "voice_name=Requested Voice" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_announcer_queue_waits_until_user_stops_speaking():
    session = FakeSession()
    session.current = FakeSpeechHandle(done=True)
    session.user_state = "speaking"
    queue = AnnouncerSpeechQueue(
        session=session,
        announcer_tts=FakeTTS(),
        silence_grace_seconds=0,
    )

    speak_task = asyncio.create_task(
        queue._speak(AnnouncerMessage(message_id="announcer-1", text="Done."))
    )
    await asyncio.sleep(0)

    assert session.say_calls == []

    session.user_state = "listening"
    queue.notify_state_changed()
    await asyncio.wait_for(speak_task, timeout=1)

    assert session.say_calls[0][0] == "Done."


@pytest.mark.asyncio
async def test_announcer_queue_restarts_wait_when_user_speaks_during_grace():
    session = FakeSession()
    session.current = FakeSpeechHandle(done=True)
    queue = AnnouncerSpeechQueue(
        session=session,
        announcer_tts=FakeTTS(),
        silence_grace_seconds=0.05,
    )

    speak_task = asyncio.create_task(
        queue._speak(AnnouncerMessage(message_id="announcer-1", text="Done."))
    )
    await asyncio.sleep(0.01)

    session.user_state = "speaking"
    queue.notify_state_changed()
    await asyncio.sleep(0.06)

    assert session.say_calls == []
    assert not speak_task.done()

    session.user_state = "listening"
    queue.notify_state_changed()
    await asyncio.wait_for(speak_task, timeout=1)

    assert session.say_calls[0][0] == "Done."


@pytest.mark.asyncio
async def test_announcer_queue_plays_audio_file_without_chat_context(
    tmp_path, monkeypatch
):
    audio_path = tmp_path / "done.wav"
    audio_path.write_bytes(b"not decoded in this test")
    session = FakeSession()
    queue = AnnouncerSpeechQueue(
        session=session,
        announcer_tts=FakeTTS(),
        silence_grace_seconds=0,
    )

    async def fake_audio_file_frames(path):
        assert path == audio_path
        if False:
            yield None

    monkeypatch.setattr(queue, "_audio_file_frames", fake_audio_file_frames)

    await queue._speak(
        AnnouncerAudioMessage(
            message_id="announcer-audio-1",
            audio_path=str(audio_path),
        )
    )

    assert session.current.waited is True
    assert session.say_handle.waited is True
    assert session.say_calls[0][0] == ""
    assert session.say_calls[0][1]["allow_interruptions"] is False
    assert session.say_calls[0][1]["add_to_chat_ctx"] is False
    assert session.say_calls[0][1]["audio"] is not None


@pytest.mark.asyncio
async def test_announcer_queue_defers_audio_file_while_user_speaks(
    tmp_path, monkeypatch
):
    audio_path = tmp_path / "done.wav"
    audio_path.write_bytes(b"not decoded in this test")
    session = FakeSession()
    session.current = FakeSpeechHandle(done=True)
    session.user_state = "speaking"
    queue = AnnouncerSpeechQueue(
        session=session,
        announcer_tts=FakeTTS(),
        silence_grace_seconds=0,
    )

    async def fake_audio_file_frames(path):
        assert path == audio_path
        if False:
            yield None

    monkeypatch.setattr(queue, "_audio_file_frames", fake_audio_file_frames)

    speak_task = asyncio.create_task(
        queue._speak(
            AnnouncerAudioMessage(
                message_id="announcer-audio-1",
                audio_path=str(audio_path),
            )
        )
    )
    await asyncio.sleep(0)

    assert session.say_calls == []

    session.user_state = "listening"
    queue.notify_state_changed()
    await asyncio.wait_for(speak_task, timeout=1)

    assert session.say_calls[0][0] == ""


class PreparedClient(CodexAppServerClient):
    def __init__(self):
        super().__init__(ws_url="ws://example.invalid", cwd="/tmp")
        self.persisted_routes = []

    async def prepare(self) -> str:
        return "dispatcher-1"

    def persist_voice_route(self, **kwargs) -> None:
        self.persisted_routes.append(kwargs)


@pytest.mark.asyncio
async def test_session_final_transcript_proactively_steers_when_logging_disabled():
    class FakeSession:
        def __init__(self):
            self.handlers = {}

        def on(self, event_name, handler):
            self.handlers[event_name] = handler

    class FakeClient:
        def __init__(self):
            self.steered_prompts = []
            self.steer_result = "turn-1"

        async def steer_active_turn(self, prompt):
            self.steered_prompts.append(prompt)
            return self.steer_result

        def has_active_prompt(self, prompt):
            return False

        def claim_speech(self, turn_id):
            return True

    session = FakeSession()
    client = FakeClient()
    router = LiveKitVoiceRouter(client)
    livekit._register_session_diagnostics(
        session,
        router,
        enable_logging=False,
    )

    session.handlers["user_input_transcribed"](
        SimpleNamespace(is_final=True, transcript=" stop now ")
    )

    for _ in range(20):
        if client.steered_prompts:
            break
        await asyncio.sleep(0.01)
    assert client.steered_prompts == ["stop now"]
    assert router.should_skip_proactively_steered_prompt("stop now") is True

    client.steer_result = None
    session.handlers["user_input_transcribed"](
        SimpleNamespace(is_final=True, transcript="start something new")
    )
    for _ in range(20):
        if len(client.steered_prompts) == 2:
            break
        await asyncio.sleep(0.01)
    assert router.should_skip_proactively_steered_prompt("start something new") is False


def test_proactive_steer_dedup_matches_formatted_twin_transcripts():
    class FakeClient:
        def has_active_prompt(self, prompt):
            return False

    router = LiveKitVoiceRouter(FakeClient())

    # STT can deliver one utterance as an unformatted final followed by a
    # formatted one; the steered version must dedupe both variants.
    router.mark_proactive_steer("im not like what did fable do")
    assert (
        router.should_skip_proactively_steered_prompt(
            "I'm not, like, what did Fable do?"
        )
        is True
    )

    router.mark_proactive_steer("what is 22")
    assert (
        router.should_skip_proactively_steered_prompt("what is two plus two") is False
    )


@pytest.mark.asyncio
async def test_voice_router_close_closes_dispatcher_client():
    # The dispatcher client holds the claude_code backend's CLI subprocess;
    # leaving it connected past the room corrupts the shared dispatcher
    # session's resume leaf once another worker takes the next call.
    class FakeDispatcherClient:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    dispatcher = FakeDispatcherClient()
    router = LiveKitVoiceRouter(dispatcher)

    await router.close()

    assert dispatcher.closed is True


@pytest.mark.asyncio
async def test_voice_router_transfers_to_prepared_target(monkeypatch, tmp_path):
    dispatcher = PreparedClient()
    prepared = []
    monkeypatch.setattr(
        config,
        "DEFAULT_DIRECT_LIVEKIT_INSTRUCTIONS_PATH",
        tmp_path / "missing-direct-instructions.md",
    )
    monkeypatch.setattr(
        voice_route,
        "SUPER_AGENT_VOICES",
        (
            livekit.CartesiaVoice("voice-a", "Alice"),
            livekit.CartesiaVoice("voice-b", "Bob"),
        ),
    )
    monkeypatch.setattr(voice_route, "SUPER_AGENT_VOICE_IDS", ("voice-a", "voice-b"))

    async def fake_prepare(self):
        prepared.append(
            (
                self._thread_id,
                self._cwd,
                self._developer_instructions,
                self._super_agent_name,
            )
        )
        return self._thread_id

    monkeypatch.setattr(SuperAgentsLiveKitClient, "prepare", fake_prepare)

    router = LiveKitVoiceRouter(dispatcher)
    await router.transfer_to_thread(
        thread_id="target-1",
        cwd="/tmp/project",
        label="Project",
    )

    assert prepared == [
        (
            "target-1",
            "/tmp/project",
            None,
            "Project",
        )
    ]
    assert router.active_client is not dispatcher
    await router.transfer_to_thread(
        thread_id="target-1",
        cwd="/tmp/project",
        label="Renamed Project",
    )
    assert prepared[-1] == (
        "target-1",
        "/tmp/project",
        None,
        "Renamed Project",
    )
    assert dispatcher.persisted_routes[-1] == {
        "active_target_thread_id": "target-1",
        "active_target_kind": "codex_thread",
        "active_target_label": "Renamed Project",
        "active_target_voice_id": stable_super_agent_voice_id(
            "target-1", "Renamed Project"
        ),
        "active_target_voice_name": livekit.stable_super_agent_voice(
            "target-1", "Renamed Project"
        ).name,
    }


def test_livekit_agent_voice_mapping_uses_route_source_of_truth():
    assert voices.stable_super_agent_voice is voice_route.stable_super_agent_voice
    assert voices.stable_super_agent_voice_id is voice_route.stable_super_agent_voice_id


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.published: list[tuple[bytes, bool, str]] = []

    async def publish_data(self, data, *, reliable, topic):
        self.published.append((data, reliable, topic))


class _FakeRoom:
    def __init__(self) -> None:
        self.local_participant = _FakeLocalParticipant()


def test_publish_agent_error_packet_publishes_status_topic():
    room = _FakeRoom()

    message_id = asyncio.run(
        livekit.publish_agent_error_packet(
            room,
            code="subscription_required",
            detail="Subscribe in Openbase Cloud.",
        )
    )

    assert len(room.local_participant.published) == 1
    data, reliable, topic = room.local_participant.published[0]
    assert reliable is True
    assert topic == config.AGENT_STATUS_TOPIC
    payload = json.loads(data.decode("utf-8"))
    assert payload == {
        "type": "agent_error",
        "code": "subscription_required",
        "detail": "Subscribe in Openbase Cloud.",
        "message_id": message_id,
    }


def test_publish_voice_lifecycle_packet_publishes_delivery_metadata():
    room = _FakeRoom()
    ledger = livekit.VoiceDeliveryLedger(
        route_snapshot=lambda: VoiceRouteSnapshot(
            route_version=2,
            active_thread_id="thread-1",
            active_voice_id="voice-1",
            active_voice_name="Corey",
            active_route="codex_thread",
        ),
        room_name="room-1",
        room_id="RM_1",
    )
    record = ledger.accept_utterance(message_id="codex-1", prompt="hello")
    record.status = "audio_delivered"
    record.turn_id = "turn-1"
    record.speech_hash = "speech-hash"
    record.speech_len = 5
    record.audio_events = 2
    record.audio_seconds = 0.1

    packet_id = asyncio.run(
        livekit.publish_voice_lifecycle_packet(
            room,
            event="safe_to_unmute",
            record=record,
        )
    )

    assert len(room.local_participant.published) == 1
    data, reliable, topic = room.local_participant.published[0]
    assert reliable is True
    assert topic == config.VOICE_LIFECYCLE_TOPIC
    payload = json.loads(data.decode("utf-8"))
    assert payload["type"] == "voice_lifecycle"
    assert payload["protocol_version"] == VOICE_LIFECYCLE_PROTOCOL_VERSION
    assert payload["event"] == "safe_to_unmute"
    assert payload["packet_id"] == packet_id
    assert payload["delivery_id"] == record.delivery_id
    assert payload["message_id"] == "codex-1"
    assert payload["turn_id"] == "turn-1"
    assert payload["status"] == "audio_delivered"
    assert payload["route"] == {
        "version": 2,
        "thread_id": "thread-1",
        "kind": "codex_thread",
        "voice_id": "voice-1",
        "voice_name": "Corey",
    }
    assert payload["prompt_len"] == 5
    assert payload["speech_hash"] == "speech-hash"
    assert payload["speech_len"] == 5
    assert payload["audio_events"] == 2
    assert payload["audio_seconds"] == 0.1


def test_publish_voice_lifecycle_packet_includes_user_turn_metadata():
    room = _FakeRoom()
    ledger = livekit.VoiceDeliveryLedger(
        route_snapshot=lambda: VoiceRouteSnapshot(
            route_version=2,
            active_thread_id="thread-1",
            active_voice_id="voice-1",
            active_voice_name="Corey",
            active_route="codex_thread",
        ),
        room_name="room-1",
        room_id="RM_1",
    )
    record = ledger.accept_utterance(message_id="codex-1", prompt="hello and")
    ledger.mark_user_turn_closed(
        record,
        decision=UserTurnClosureDecision(
            confidence=0.35,
            source="semantic_tail",
            delay_seconds=3.5,
            completion_reason="continuation_tail",
        ),
    )
    record.user_turn_eou_probability = 0.21
    record.user_turn_silence_ms = 3500
    record.user_turn_transcript_confidence = 0.88
    record.user_turn_transcription_delay_ms = 120

    asyncio.run(
        livekit.publish_voice_lifecycle_packet(
            room,
            event="safe_to_mute_user",
            record=record,
            reason="continuation_tail",
        )
    )

    data, _reliable, topic = room.local_participant.published[0]
    assert topic == config.VOICE_LIFECYCLE_TOPIC
    payload = json.loads(data.decode("utf-8"))
    assert payload["event"] == "safe_to_mute_user"
    assert payload["reason"] == "continuation_tail"
    assert payload["user_turn"] == {
        "confidence": 0.35,
        "source": "semantic_tail",
        "delay_ms": 3500,
        "eou_probability": 0.21,
        "silence_ms": 3500,
        "transcript_confidence": 0.88,
        "transcription_delay_ms": 120,
        "transcript_len": 9,
        "text_hash": record.prompt_hash,
        "completion_reason": "continuation_tail",
    }


def test_publish_voice_lifecycle_packet_stringifies_room_id_guardrail():
    room = _FakeRoom()
    ledger = livekit.VoiceDeliveryLedger(
        route_snapshot=lambda: VoiceRouteSnapshot(
            route_version=2,
            active_thread_id="thread-1",
            active_voice_id=None,
            active_voice_name=None,
            active_route="dispatcher",
        ),
        room_name="room-1",
        room_id=object(),
    )
    record = ledger.accept_utterance(message_id="codex-1", prompt="hello")

    asyncio.run(
        livekit.publish_voice_lifecycle_packet(
            room,
            event="utterance_accepted",
            record=record,
        )
    )

    data, _reliable, topic = room.local_participant.published[0]
    assert topic == config.VOICE_LIFECYCLE_TOPIC
    payload = json.loads(data.decode("utf-8"))
    assert isinstance(payload["room_id"], str)


def test_room_sid_resolves_awaitable_livekit_property():
    class RoomWithAwaitableSid:
        @property
        async def sid(self):
            return "RM_async"

    assert asyncio.run(livekit._room_sid(RoomWithAwaitableSid())) == "RM_async"


def test_cloud_audio_handshake_error_is_clear_and_redacted():
    class CloudAudioHandshakeError(RuntimeError):
        status = 403

    exc = CloudAudioHandshakeError(
        "403, message='Forbidden', url='wss://app.openbase.cloud/api/openbase/audio/cartesia/tts/websocket?token=secret-token', "
        "headers={'X-API-Key': 'machine-secret', 'Authorization': 'Bearer access-secret'}"
    )

    assert livekit._agent_error_code(exc) == "cloud_audio_auth_failed"
    detail = livekit._agent_error_detail(exc)
    assert "Openbase Cloud audio authorization failed" in detail
    assert "machine-secret" not in detail
    assert "access-secret" not in detail
    assert "secret-token" not in livekit.exception_chain_summary(exc)


@pytest.mark.asyncio
async def test_session_error_reports_agent_status_when_logging_disabled():
    class FakeSession:
        def __init__(self):
            self.handlers = {}

        def on(self, event_name, handler):
            self.handlers[event_name] = handler

    session = FakeSession()
    room = _FakeRoom()
    router = LiveKitVoiceRouter(PreparedClient())
    livekit._register_session_diagnostics(
        session,
        router,
        enable_logging=False,
        on_unrecoverable_error=lambda exc: livekit._report_agent_error(room, exc),
    )

    session.handlers["error"](
        SimpleNamespace(error=RuntimeError("provider startup failed"))
    )

    for _ in range(20):
        if room.local_participant.published:
            break
        await asyncio.sleep(0.01)

    assert len(room.local_participant.published) == 1
    data, _reliable, topic = room.local_participant.published[0]
    assert topic == config.AGENT_STATUS_TOPIC
    payload = json.loads(data.decode("utf-8"))
    assert payload["type"] == "agent_error"
    assert payload["code"] == "agent_start_failed"
    assert "provider startup failed" in payload["detail"]


def test_verify_cloud_audio_subscription_reports_lapsed_subscription(monkeypatch):
    room = _FakeRoom()
    spoken: list[str] = []
    session = SimpleNamespace(say=lambda text: spoken.append(text))

    def raise_subscription_error(**_kwargs):
        raise livekit.OpenbaseCloudAudioSubscriptionError(
            "Subscribe in Openbase Cloud to use managed audio."
        )

    monkeypatch.setattr(
        livekit,
        "ensure_openbase_cloud_audio_subscription",
        raise_subscription_error,
    )
    monkeypatch.setattr(livekit, "selected_tts_provider_id", lambda: "openbase_cloud")
    monkeypatch.setattr(livekit, "selected_stt_provider_id", lambda: "openbase_cloud")

    asyncio.run(livekit._verify_cloud_audio_subscription(room, session))

    assert len(room.local_participant.published) == 1
    data, _reliable, topic = room.local_participant.published[0]
    assert topic == config.AGENT_STATUS_TOPIC
    payload = json.loads(data.decode("utf-8"))
    assert payload["type"] == "agent_error"
    assert payload["code"] == "subscription_required"
    assert payload["detail"] == "Subscribe in Openbase Cloud to use managed audio."
    assert spoken and "Openbase Cloud audio is unavailable" in spoken[0]


def test_verify_cloud_audio_subscription_skips_transient_errors(monkeypatch):
    room = _FakeRoom()
    session = SimpleNamespace(
        say=lambda text: pytest.fail("should not speak on transient errors")
    )

    def raise_transient_error(**_kwargs):
        raise livekit.AuthTransientError("backend briefly unavailable")

    monkeypatch.setattr(
        livekit,
        "ensure_openbase_cloud_audio_subscription",
        raise_transient_error,
    )
    monkeypatch.setattr(livekit, "selected_tts_provider_id", lambda: "openbase_cloud")
    monkeypatch.setattr(livekit, "selected_stt_provider_id", lambda: "openbase_cloud")

    asyncio.run(livekit._verify_cloud_audio_subscription(room, session))

    assert room.local_participant.published == []
