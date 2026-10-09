"""Engine selection wiring in the LiveKit agent entrypoint.

Fakes the room, the job context and ``AgentSession``; proves the live engine
is built without STT/TTS and with the barge-in VAD, that the voice-engine
attribute and the non-fatal fallback packet are published, and that the
pipeline path is untouched.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from openbase_coder_cli.livekit_agent import config, livekit
from openbase_coder_cli.livekit_agent.live_voice import (
    LIVE_VOICE_PROVIDER_FAILED_CODE,
    LIVE_VOICE_UNAVAILABLE_CODE,
    LiveVoiceCredentials,
    LiveVoiceSessionError,
    VoiceEngineDecision,
)


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.published: list[tuple[bytes, bool, str]] = []
        self.attributes: dict[str, str] = {}

    async def publish_data(self, data, *, reliable, topic):
        self.published.append((data, reliable, topic))

    async def set_attributes(self, attributes):
        self.attributes.update(attributes)


class _FakeRoom:
    def __init__(self) -> None:
        self.name = "room-1"
        self.sid = "RM_1"
        self.local_participant = _FakeLocalParticipant()
        self.handlers: dict[str, list] = {}
        self.connection_state = None

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def off(self, event, handler):
        self.handlers.get(event, []).remove(handler)


class _FakeAgentSession:
    instances: list["_FakeAgentSession"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.handlers: dict[str, list] = {}
        self.started_with = None
        self.closed = False
        self.user_state = "listening"
        self.agent_state = "listening"
        _FakeAgentSession.instances.append(self)

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def off(self, event, handler):
        if handler in self.handlers.get(event, []):
            self.handlers[event].remove(handler)

    async def start(self, *, agent, room):
        self.started_with = (agent, room)
        await agent.on_enter()

    async def aclose(self):
        self.closed = True


class _FakeLiveSession:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.appends: list[tuple[str, str, str | None]] = []

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def off(self, event, handler):
        self.handlers[event].remove(handler)

    def append_commentary(self, text, *, delegation_id=None):
        self.appends.append(("commentary", text, delegation_id))

    def append_thinking(self, text, *, delegation_id=None):
        self.appends.append(("thinking", text, delegation_id))

    def append_instructions(self, text, *, delegation_id=None):
        self.appends.append(("instructions", text, delegation_id))


class _FakeGPTLiveModel:
    instances: list["_FakeGPTLiveModel"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        _FakeGPTLiveModel.instances.append(self)


class _FakeBackendClient:
    _thread_id = "dispatcher-thread"

    async def prepare(self):
        return "dispatcher-thread"

    async def aclose(self):
        pass

    def set_orphaned_result_handler(self, handler):
        self.orphan_handler = handler

    def reset_voice_route_to_dispatcher(self):
        pass


def _fake_ctx():
    room = _FakeRoom()
    shutdown_callbacks: list = []

    async def connect(**_kwargs):
        return None

    return SimpleNamespace(
        room=room,
        proc=SimpleNamespace(userdata={"vad": "fake-vad"}),
        connect=connect,
        add_shutdown_callback=shutdown_callbacks.append,
        shutdown_callbacks=shutdown_callbacks,
        log_context_fields={},
        shutdown=lambda reason="": None,
    )


def _live_decision() -> VoiceEngineDecision:
    return VoiceEngineDecision(
        engine="live",
        credentials=LiveVoiceCredentials(
            base_url="https://cloud.example/api/openbase/live/openai/v1",
            api_key="cloud-token",
        ),
    )


@pytest.fixture
def wiring(monkeypatch):
    _FakeAgentSession.instances.clear()
    _FakeGPTLiveModel.instances.clear()
    fake_live = _FakeLiveSession()
    pipeline_calls: list = []
    pipeline_wiring: list = []

    async def fake_start_pipeline(ctx, voice_router, delivery_ledger):
        pipeline_calls.append(delivery_ledger)
        return _FakeAgentSession(), "announcer-tts", ()

    async def no_stall_loop(**_kwargs):
        return None

    monkeypatch.setattr(livekit, "AgentSession", _FakeAgentSession)
    monkeypatch.setattr(livekit, "import_live_model", lambda: _FakeGPTLiveModel)
    monkeypatch.setattr(livekit, "_diagnostic_vad", lambda vad: vad)
    monkeypatch.setattr(livekit, "_shared_voice_backend_client", _FakeBackendClient())
    monkeypatch.setattr(livekit, "_refresh_audio_credentials", lambda: None)
    monkeypatch.setattr(livekit, "_start_voice_session", fake_start_pipeline)
    monkeypatch.setattr(
        livekit,
        "_wire_pipeline_voice_call",
        lambda *args, **kwargs: pipeline_wiring.append(args),
    )
    monkeypatch.setattr(
        livekit.LiveVoiceAssistant, "duplex_session", property(lambda self: fake_live)
    )
    from openbase_coder_cli.livekit_agent import stall_diagnostics

    monkeypatch.setattr(stall_diagnostics, "stall_watch_loop", no_stall_loop)
    return SimpleNamespace(
        live=fake_live, pipeline_calls=pipeline_calls, pipeline_wiring=pipeline_wiring
    )


def _status_packets(room: _FakeRoom) -> list[dict]:
    return [
        json.loads(data.decode("utf-8"))
        for data, _reliable, topic in room.local_participant.published
        if topic == config.AGENT_STATUS_TOPIC
    ]


async def _run_entrypoint(ctx, decision, monkeypatch):
    async def decide():
        return decision

    monkeypatch.setattr(livekit, "_decide_voice_engine_for_call", decide)
    await livekit.livekit_agent(ctx)
    await asyncio.sleep(0)


async def test_live_engine_builds_a_duplex_session_and_publishes_the_attribute(
    wiring, monkeypatch
):
    ctx = _fake_ctx()
    await _run_entrypoint(ctx, _live_decision(), monkeypatch)

    assert wiring.pipeline_calls == []
    (model,) = _FakeGPTLiveModel.instances
    assert model.kwargs == {
        "model": config.LIVE_VOICE_MODEL,
        "voice": config.LIVE_VOICE_DEFAULT_VOICE,
        "delegation": "client",
        "api_key": "cloud-token",
        "base_url": "https://cloud.example/api/openbase/live/openai/v1",
    }
    (session,) = _FakeAgentSession.instances
    assert session.kwargs["llm"] is model
    assert session.kwargs["vad"] == "fake-vad"
    assert "stt" not in session.kwargs and "tts" not in session.kwargs
    assert session.kwargs["turn_handling"] == {"interruption": {"mode": "vad"}}
    agent, room = session.started_with
    assert isinstance(agent, livekit.LiveVoiceAssistant)
    assert agent.instructions == config.live_voice_startup_instructions()
    assert agent.instructions.startswith(config.LIVE_VOICE_STARTUP_INSTRUCTIONS)
    # The bridge subscribed to the plugin session once the agent entered:
    # every closed caller utterance goes to the agent, delegations bind.
    assert len(wiring.live.handlers["input_audio_transcription_completed"]) == 1
    assert len(wiring.live.handlers["delegation_created"]) == 1
    assert (
        ctx.room.local_participant.attributes[config.VOICE_ENGINE_ATTRIBUTE] == "live"
    )
    assert _status_packets(ctx.room) == []
    # Live-mode session plumbing: speaking state feeds the bridge; transcripts
    # reach it only from the plugin session, so each utterance is sent once.
    assert "agent_state_changed" in session.handlers
    assert "data_received" in ctx.room.handlers
    for callback in ctx.shutdown_callbacks:
        await callback()


async def test_pipeline_engine_is_untouched_and_publishes_pipeline(wiring, monkeypatch):
    ctx = _fake_ctx()
    await _run_entrypoint(ctx, VoiceEngineDecision(engine="pipeline"), monkeypatch)

    assert _FakeGPTLiveModel.instances == []
    assert len(wiring.pipeline_calls) == 1
    assert wiring.pipeline_calls[0].live_mode is False
    assert len(wiring.pipeline_wiring) == 1
    assert (
        ctx.room.local_participant.attributes[config.VOICE_ENGINE_ATTRIBUTE]
        == "pipeline"
    )
    assert _status_packets(ctx.room) == []


async def test_live_fallback_publishes_a_non_fatal_notice_and_keeps_auto_mute(
    wiring, monkeypatch
):
    ctx = _fake_ctx()
    decision = VoiceEngineDecision(
        engine="pipeline",
        fallback_reason="gateway_not_deployed",
        fallback_detail="HTTP 404 from the gateway.",
    )
    await _run_entrypoint(ctx, decision, monkeypatch)

    assert len(wiring.pipeline_calls) == 1
    assert (
        ctx.room.local_participant.attributes[config.VOICE_ENGINE_ATTRIBUTE]
        == "pipeline"
    )
    (packet,) = _status_packets(ctx.room)
    assert packet["type"] == "agent_error"
    assert packet["code"] == LIVE_VOICE_UNAVAILABLE_CODE
    assert packet["severity"] == "warning"
    assert "gateway_not_deployed" in packet["detail"]
    assert "HTTP 404" in packet["detail"]


async def test_live_session_start_failure_falls_back_to_the_pipeline(
    wiring, monkeypatch
):
    ctx = _fake_ctx()

    async def failing_start(ctx, voice_router, delivery_ledger, decision):
        raise RuntimeError("OpenAI Live API connection error")

    monkeypatch.setattr(livekit, "_start_live_voice_session", failing_start)
    await _run_entrypoint(ctx, _live_decision(), monkeypatch)

    assert len(wiring.pipeline_calls) == 1
    assert wiring.pipeline_calls[0].live_mode is False
    assert (
        ctx.room.local_participant.attributes[config.VOICE_ENGINE_ATTRIBUTE]
        == "pipeline"
    )
    (packet,) = _status_packets(ctx.room)
    assert packet["code"] == LIVE_VOICE_UNAVAILABLE_CODE
    assert packet["severity"] == "warning"
    assert "live_session_start_failed" in packet["detail"]
    assert "connection error" in packet["detail"]


async def test_pipeline_failure_after_fallback_is_still_fatal(wiring, monkeypatch):
    ctx = _fake_ctx()
    ended: list = []

    async def failing_pipeline(ctx, voice_router, delivery_ledger):
        raise RuntimeError("Cartesia API key is required")

    async def record_end(ctx, exc):
        ended.append(exc)

    monkeypatch.setattr(livekit, "_start_voice_session", failing_pipeline)
    monkeypatch.setattr(livekit, "_end_call_after_agent_error", record_end)
    decision = VoiceEngineDecision(
        engine="pipeline", fallback_reason="login_required", fallback_detail="no login"
    )
    with pytest.raises(RuntimeError):
        await _run_entrypoint(ctx, decision, monkeypatch)
    assert len(ended) == 1 and "Cartesia" in str(ended[0])


def test_live_session_errors_map_to_their_own_status_code():
    exc = LiveVoiceSessionError(RuntimeError("GPT-Live connection closed unexpectedly"))
    assert livekit._agent_error_code(exc) == LIVE_VOICE_PROVIDER_FAILED_CODE
    detail = livekit._agent_error_detail(exc)
    assert "live voice connection was lost" in detail
    assert "classic pipeline" in detail


def test_live_start_passes_proactive_steering_off(monkeypatch):
    """The bridge owns thread submissions; session diagnostics must not steer."""
    captured: dict = {}

    def fake_register(session, voice_router, **kwargs):
        captured.update(kwargs)
        return ()

    monkeypatch.setattr(livekit, "AgentSession", _FakeAgentSession)
    monkeypatch.setattr(livekit, "import_live_model", lambda: _FakeGPTLiveModel)
    monkeypatch.setattr(livekit, "_diagnostic_vad", lambda vad: vad)
    monkeypatch.setattr(livekit, "_register_session_diagnostics", fake_register)
    fake_live = _FakeLiveSession()
    monkeypatch.setattr(
        livekit.LiveVoiceAssistant, "duplex_session", property(lambda self: fake_live)
    )
    ctx = _fake_ctx()
    router = livekit.LiveKitVoiceRouter(_FakeBackendClient())
    ledger = livekit.VoiceDeliveryLedger(
        route_snapshot=router.route_snapshot, live_mode=True
    )

    async def run():
        return await livekit._start_live_voice_session(
            ctx, router, ledger, _live_decision()
        )

    session, bridge, handlers = asyncio.run(run())
    assert captured["proactive_steering"] is False
    assert captured["enable_logging"] is config.LIVEKIT_VERBOSE_LOGGING
    assert handlers == ()
    assert len(fake_live.handlers["input_audio_transcription_completed"]) == 1


def test_live_startup_instructions_never_let_the_voice_model_answer_itself():
    """GPT-Live voices the agent; it must not answer from its own knowledge."""
    text = config.LIVE_VOICE_STARTUP_INSTRUCTIONS
    assert "Never answer a question or request yourself" in text
    assert "no general knowledge" in text
    assert "desktop" in text and "files" in text
    assert "Only the agent answers." in text
    assert "relay commentary faithfully" in text.lower()


def test_live_voice_persona_names_the_host_kind_for_both_kinds():
    from openbase_coder_cli import host_kind

    cloud = config.live_voice_startup_instructions(host_kind.HOST_KIND_CLOUD_WORKSPACE)
    assert cloud.startswith(config.LIVE_VOICE_STARTUP_INSTRUCTIONS)
    assert "Openbase Cloud workspace" in cloud
    assert "never as their desktop or their Mac" in cloud
    mac = config.live_voice_startup_instructions(host_kind.HOST_KIND_MAC)
    assert mac.startswith(config.LIVE_VOICE_STARTUP_INSTRUCTIONS)
    assert "runs on their own Mac" in mac
    assert "cloud workspace" not in mac.lower()
    # Still the voice, never the brain: the host line adds no answering licence.
    assert "Never answer a question or request yourself" in cloud
