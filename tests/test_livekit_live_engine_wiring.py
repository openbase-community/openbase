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
        llm = self.kwargs.get("llm")
        create = getattr(llm, "session", None)
        self.live_session = create() if create is not None else None
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


class _FakePluginSession:
    """What GPTLiveModel.session() returns: connects in a task on creation."""

    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.closed = False
        self._main_atask = asyncio.get_running_loop().create_future()

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def fail(self, error) -> None:
        for handler in self.handlers.get("error", []):
            handler(error)
        self._main_atask.set_result(None)

    async def aclose(self):
        self.closed = True
        if not self._main_atask.done():
            self._main_atask.cancel()


class _FakeGPTLiveModel:
    async def aclose(self):
        self.closed = True

    instances: list["_FakeGPTLiveModel"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.sessions: list[_FakePluginSession] = []
        _FakeGPTLiveModel.instances.append(self)

    def session(self) -> _FakePluginSession:
        session = _FakePluginSession()
        self.sessions.append(session)
        return session


class _FakeBackendClient:
    _thread_id = "dispatcher-thread"

    def __init__(self) -> None:
        self.warm_calls = 0

    async def prepare(self):
        return "dispatcher-thread"

    async def warm(self):
        self.warm_calls += 1
        return True

    async def aclose(self):
        pass

    def set_orphaned_result_handler(self, handler):
        self.orphan_handler = handler

    def reset_voice_route_to_dispatcher(self):
        pass


def _fake_ctx():
    room = _FakeRoom()
    shutdown_callbacks: list = []
    shutdowns: list[str] = []

    async def connect(**_kwargs):
        return None

    return SimpleNamespace(
        room=room,
        proc=SimpleNamespace(userdata={"vad": "fake-vad"}),
        connect=connect,
        add_shutdown_callback=shutdown_callbacks.append,
        shutdown_callbacks=shutdown_callbacks,
        log_context_fields={},
        shutdown=lambda reason="": shutdowns.append(reason),
        shutdowns=shutdowns,
        job=SimpleNamespace(
            id="AJ_1",
            dispatch_id="AD_1",
            metadata="",
            room=SimpleNamespace(creation_time=0, creation_time_ms=0),
        ),
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
    real_start_pipeline = livekit._start_voice_session

    async def fake_start_pipeline(ctx, voice_router, delivery_ledger):
        pipeline_calls.append(delivery_ledger)
        return _FakeAgentSession(), "announcer-tts", ()

    async def no_stall_loop(**_kwargs):
        return None

    backend = _FakeBackendClient()
    monkeypatch.setattr(livekit, "AgentSession", _FakeAgentSession)
    monkeypatch.setattr(livekit, "import_live_model", lambda: _FakeGPTLiveModel)
    monkeypatch.setattr(livekit, "_diagnostic_vad", lambda vad: vad)
    monkeypatch.setattr(livekit, "_shared_voice_backend_client", backend)
    monkeypatch.setattr(livekit, "_refresh_audio_credentials", lambda: None)
    monkeypatch.setattr(livekit, "_start_voice_session", fake_start_pipeline)
    monkeypatch.setattr(livekit, "LIVEKIT_PARTICIPANT_JOIN_TIMEOUT_SECONDS", 60.0)
    monkeypatch.setattr(
        livekit, "_live_voice_readiness_cache", livekit.LiveVoiceReadinessCache()
    )
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
        live=fake_live,
        pipeline_calls=pipeline_calls,
        pipeline_wiring=pipeline_wiring,
        backend=backend,
        real_start_pipeline=real_start_pipeline,
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
    wiring, monkeypatch, tmp_path
):
    from openbase_coder_cli.services import livekit_pool_activity

    monkeypatch.setattr(livekit_pool_activity, "_ACTIVITY_DIR", tmp_path / "activity")
    assert livekit_pool_activity.activity_timestamp("job") == 0
    ctx = _fake_ctx()
    await _run_entrypoint(ctx, _live_decision(), monkeypatch)
    assert livekit_pool_activity.activity_timestamp("job") > 0

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
    # Start-up latency: the gateway connection was opened before the session
    # started (preconnect) and AgentSession.start adopted that very session;
    # the dispatcher backend was warmed in the background.
    assert len(model.sessions) == 1
    assert session.live_session is model.sessions[0]
    assert "error" in model.sessions[0].handlers
    assert wiring.backend.warm_calls == 1
    # The caller's arrival is tracked for the join timeout and the timing log.
    assert "participant_connected" in ctx.room.handlers
    for callback in ctx.shutdown_callbacks:
        await callback()


async def test_live_hangup_ends_job_and_closes_character_resources(wiring, monkeypatch):
    from livekit.agents.voice.events import CloseEvent, CloseReason

    deleted = []

    async def delete_room(name):
        deleted.append(name)

    monkeypatch.setattr(livekit, "_delete_room", delete_room)
    ctx = _fake_ctx()
    await _run_entrypoint(ctx, _live_decision(), monkeypatch)
    (session,) = _FakeAgentSession.instances
    for handler in tuple(session.handlers["close"]):
        handler(CloseEvent(reason=CloseReason.PARTICIPANT_DISCONNECTED))
    await asyncio.sleep(0)
    assert deleted == ["room-1"]
    assert ctx.shutdowns == ["caller-disconnected"]
    for callback in ctx.shutdown_callbacks:
        await callback()
    assert session.closed
    assert _FakeGPTLiveModel.instances[0].closed
    assert not ctx.room.handlers["data_received"]
    assert not wiring.live.handlers["input_audio_transcription_completed"]


async def test_pipeline_hangup_ends_job_after_real_pipeline_construction(wiring, monkeypatch):
    from unittest.mock import AsyncMock

    from livekit.agents.voice.events import CloseEvent, CloseReason

    # Exercise the production builder: mocking _start_voice_session would
    # miss a cleanup hook installed only on the GPT-Live path.
    voice = SimpleNamespace(id="voice", name="Voice")
    provider = SimpleNamespace(provider_id="test", default_announcer_voice=lambda: voice)
    monkeypatch.setattr(livekit, "get_tts_provider", lambda _: provider)
    monkeypatch.setattr(livekit, "VoiceSelectingTTS", lambda **_: object())
    monkeypatch.setattr(livekit, "_build_stt", lambda _: object())
    monkeypatch.setattr(livekit, "CodexLiveKitLLM", lambda *args, **kwargs: object())
    monkeypatch.setattr(livekit, "SafeMultilingualModel", lambda **_: object())
    monkeypatch.setattr(livekit, "_register_session_diagnostics", lambda *args, **kwargs: ())
    delete = AsyncMock()
    monkeypatch.setattr(livekit, "_delete_room", delete)
    ctx = _fake_ctx()
    session, _, _ = await wiring.real_start_pipeline(ctx, SimpleNamespace(), None)
    assert session.started_with[1] is ctx.room
    for handler in tuple(session.handlers["close"]):
        handler(CloseEvent(reason=CloseReason.PARTICIPANT_DISCONNECTED))
    await asyncio.sleep(0)
    delete.assert_awaited_once_with("room-1")
    assert ctx.shutdowns == ["caller-disconnected"]
    for callback in ctx.shutdown_callbacks:
        await callback()
    assert not session.handlers["close"]


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

    async def failing_start(ctx, voice_router, delivery_ledger, decision, **kwargs):
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
    assert [name for name, _ in handlers] == ["user_state_changed"]
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
    assert "Let the agent determine access to other devices" in cloud
    mac = config.live_voice_startup_instructions(host_kind.HOST_KIND_MAC)
    assert mac.startswith(config.LIVE_VOICE_STARTUP_INSTRUCTIONS)
    assert "runs on their own Mac" in mac
    assert "cloud workspace" not in mac.lower()
    # Still the voice, never the brain: the host line adds no answering licence.
    assert "Never answer a question or request yourself" in cloud


# --- start-up latency ---------------------------------------------------------------


async def test_live_session_start_failure_closes_the_preconnected_session_and_clears_readiness(
    wiring, monkeypatch
):
    ctx = _fake_ctx()
    cache = livekit._live_voice_readiness_cache
    decision = _live_decision()
    cache.store(decision.credentials, tts_provider_id="t", stt_provider_id="s")

    class _FailingAgentSession(_FakeAgentSession):
        async def start(self, *, agent, room):
            raise RuntimeError("OpenAI Live API connection error")

    monkeypatch.setattr(livekit, "AgentSession", _FailingAgentSession)
    await _run_entrypoint(ctx, decision, monkeypatch)

    (model,) = _FakeGPTLiveModel.instances
    (preconnected,) = model.sessions
    assert preconnected.closed is True
    assert model.closed is True
    assert (
        cache.get(decision.credentials, tts_provider_id="t", stt_provider_id="s")
        is None
    )
    assert len(wiring.pipeline_calls) == 1
    (packet,) = _status_packets(ctx.room)
    assert "live_session_start_failed" in packet["detail"]


@pytest.mark.parametrize("failure", ["error", "closed", "timeout"])
async def test_gateway_startup_failure_after_framework_start_falls_back(
    wiring, monkeypatch, failure
):
    ctx = _fake_ctx()
    ended = []

    async def record_end(ctx, exc):
        ended.append(exc)

    async def connection():
        if failure == "timeout":
            await asyncio.Event().wait()
        if failure == "error":
            raise RuntimeError("gateway rejected startup")

    monkeypatch.setattr(livekit, "_end_call_after_agent_error", record_end)
    monkeypatch.setattr(livekit, "LIVE_VOICE_PREFLIGHT_TIMEOUT_SECONDS", 0.01)
    wiring.live._session_started_fut = asyncio.get_running_loop().create_future()
    wiring.live._main_atask = asyncio.create_task(connection())
    try:
        await _run_entrypoint(ctx, _live_decision(), monkeypatch)
        assert len(wiring.pipeline_calls) == 1
        assert not ended and not ctx.shutdowns
        (packet,) = _status_packets(ctx.room)
        assert "live_session_start_failed" in packet["detail"]
        assert packet["severity"] == "warning"
    finally:
        wiring.live._main_atask.cancel()
        await asyncio.gather(wiring.live._main_atask, return_exceptions=True)
        for callback in ctx.shutdown_callbacks:
            await callback()


async def test_preconnect_is_skipped_when_disabled(wiring, monkeypatch):
    monkeypatch.setattr(livekit, "LIVE_VOICE_PRECONNECT", False)
    ctx = _fake_ctx()
    await _run_entrypoint(ctx, _live_decision(), monkeypatch)
    (model,) = _FakeGPTLiveModel.instances
    (session,) = _FakeAgentSession.instances
    # The framework still gets a session, created at start as before.
    assert len(model.sessions) == 1 and session.live_session is model.sessions[0]
    assert type(model) is _FakeGPTLiveModel
    for callback in ctx.shutdown_callbacks:
        await callback()


async def test_preconnected_session_that_failed_is_replaced_by_a_fresh_one():
    model_cls = livekit._preconnecting_model_class(_FakeGPTLiveModel)
    assert livekit._preconnecting_model_class(_FakeGPTLiveModel) is model_cls
    model = model_cls(
        model="m", voice="v", delegation="client", api_key="k", base_url="u"
    )
    first = model.preconnect()
    assert model.preconnect() is first
    first.fail(RuntimeError("gateway closed"))
    replacement = model.session()
    await asyncio.sleep(0)
    assert replacement is not first and first.closed is True
    assert model.sessions == [first, replacement]
    # Nothing pending any more: discard is a no-op and session() creates anew.
    await model.discard_preconnected()
    assert model.session() is not replacement


async def test_discard_preconnected_closes_an_unused_session():
    model_cls = livekit._preconnecting_model_class(_FakeGPTLiveModel)
    model = model_cls(
        model="m", voice="v", delegation="client", api_key="k", base_url="u"
    )
    pending = model.preconnect()
    await model.discard_preconnected()
    assert pending.closed is True


async def test_a_connection_failure_discards_the_preconnected_session(
    wiring, monkeypatch
):
    ctx = _fake_ctx()

    async def failing_connect(**_kwargs):
        raise RuntimeError("signal connection refused")

    ctx.connect = failing_connect
    with pytest.raises(RuntimeError):
        await _run_entrypoint(ctx, _live_decision(), monkeypatch)
    for model in _FakeGPTLiveModel.instances:
        assert all(session.closed for session in model.sessions)


async def test_discard_without_a_prepared_live_model():
    task = asyncio.create_task(asyncio.sleep(0, result=None))
    await task
    await livekit._discard_live_voice_model(task)


async def test_route_identity_failure_closes_the_early_model(wiring, monkeypatch):
    from openbase_coder_cli import voice_identity

    def invalid_identity(router):
        raise ValueError("Unknown agent voice")

    monkeypatch.setattr(voice_identity, "route_voice_identity", invalid_identity)
    ctx = _fake_ctx()
    await _run_entrypoint(ctx, _live_decision(), monkeypatch)
    (model,) = _FakeGPTLiveModel.instances
    assert model.closed
    assert all(session.closed for session in model.sessions)
    assert len(wiring.pipeline_calls) == 1


async def test_call_nobody_joins_ends_itself(wiring, monkeypatch):
    deleted: list[str] = []

    async def fake_delete(room_name):
        deleted.append(room_name)

    monkeypatch.setattr(livekit, "_delete_room", fake_delete)
    monkeypatch.setattr(livekit, "LIVEKIT_PARTICIPANT_JOIN_TIMEOUT_SECONDS", 0.01)
    ctx = _fake_ctx()
    await _run_entrypoint(ctx, VoiceEngineDecision(engine="pipeline"), monkeypatch)
    await asyncio.sleep(0.05)
    assert deleted == ["room-1"]
    assert ctx.shutdowns == ["participant-join-timeout"]
    for callback in ctx.shutdown_callbacks:
        await callback()


async def test_a_caller_joining_in_time_keeps_the_call(wiring, monkeypatch):
    deleted: list[str] = []

    async def fake_delete(room_name):
        deleted.append(room_name)

    monkeypatch.setattr(livekit, "_delete_room", fake_delete)
    monkeypatch.setattr(livekit, "LIVEKIT_PARTICIPANT_JOIN_TIMEOUT_SECONDS", 0.02)
    ctx = _fake_ctx()
    await _run_entrypoint(ctx, VoiceEngineDecision(engine="pipeline"), monkeypatch)
    for handler in ctx.room.handlers["participant_connected"]:
        handler(
            SimpleNamespace(
                identity="caller",
                kind=livekit.rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
            )
        )
    await asyncio.sleep(0.05)
    assert deleted == [] and ctx.shutdowns == []
    for callback in ctx.shutdown_callbacks:
        await callback()


async def test_a_caller_already_in_the_room_counts_as_joined(wiring, monkeypatch):
    deleted: list[str] = []

    async def fake_delete(room_name):
        deleted.append(room_name)

    monkeypatch.setattr(livekit, "_delete_room", fake_delete)
    monkeypatch.setattr(livekit, "LIVEKIT_PARTICIPANT_JOIN_TIMEOUT_SECONDS", 0.01)
    ctx = _fake_ctx()
    ctx.room.remote_participants = {
        "caller": SimpleNamespace(
            identity="caller",
            kind=livekit.rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
        )
    }
    await _run_entrypoint(ctx, VoiceEngineDecision(engine="pipeline"), monkeypatch)
    await asyncio.sleep(0.05)
    assert deleted == [] and ctx.shutdowns == []
    for callback in ctx.shutdown_callbacks:
        await callback()


async def test_warm_failures_never_reach_the_call(wiring, monkeypatch):
    async def failing_warm():
        raise RuntimeError("claude not installed")

    wiring.backend.warm = failing_warm
    ctx = _fake_ctx()
    await _run_entrypoint(ctx, VoiceEngineDecision(engine="pipeline"), monkeypatch)
    await asyncio.sleep(0)
    assert len(wiring.pipeline_calls) == 1
    for callback in ctx.shutdown_callbacks:
        await callback()


async def test_another_agent_does_not_disable_the_caller_timeout(wiring, monkeypatch):
    deleted = []

    async def fake_delete(room_name):
        deleted.append(room_name)

    monkeypatch.setattr(livekit, "_delete_room", fake_delete)
    monkeypatch.setattr(livekit, "LIVEKIT_PARTICIPANT_JOIN_TIMEOUT_SECONDS", 0.01)
    ctx = _fake_ctx()
    ctx.room.remote_participants = {
        "agent": SimpleNamespace(
            identity="agent",
            kind=livekit.rtc.ParticipantKind.PARTICIPANT_KIND_AGENT,
        )
    }
    await _run_entrypoint(ctx, VoiceEngineDecision(engine="pipeline"), monkeypatch)
    await asyncio.sleep(0.05)
    assert deleted == ["room-1"]
    assert ctx.shutdowns == ["participant-join-timeout"]
    for callback in ctx.shutdown_callbacks:
        await callback()


async def test_dispatcher_warmup_can_be_disabled(wiring, monkeypatch):
    monkeypatch.setattr(livekit, "LIVEKIT_DISPATCHER_WARMUP", False)

    async def unexpected_warm():
        pytest.fail("dispatcher warm-up is disabled")

    wiring.backend.warm = unexpected_warm
    ctx = _fake_ctx()
    await _run_entrypoint(ctx, VoiceEngineDecision(engine="pipeline"), monkeypatch)
    await asyncio.sleep(0)
    for callback in ctx.shutdown_callbacks:
        await callback()


def test_prewarm_starts_the_readiness_refresher(monkeypatch):
    started: list[bool] = []
    monkeypatch.setattr(livekit, "install_vad_backlog_patch", lambda: None)
    monkeypatch.setattr(livekit.silero.VAD, "load", staticmethod(lambda: "vad"))
    monkeypatch.setattr(
        livekit,
        "_live_voice_readiness_refresher",
        SimpleNamespace(start=lambda: started.append(True)),
    )
    proc = SimpleNamespace(userdata={})
    monkeypatch.setattr(livekit, "LIVE_VOICE_READINESS_PREWARM", True)
    livekit.prewarm(proc)
    assert started == [True] and proc.userdata["vad"] is not None
    monkeypatch.setattr(livekit, "LIVE_VOICE_READINESS_PREWARM", False)
    livekit.prewarm(proc)
    assert started == [True]


def test_unavailable_live_plugin_keeps_pipeline_vad_without_background_probe(
    monkeypatch,
):
    monkeypatch.setattr(livekit, "install_vad_backlog_patch", lambda: None)
    monkeypatch.setattr(livekit.silero.VAD, "load", staticmethod(lambda: "vad"))
    monkeypatch.setattr(livekit, "LIVE_VOICE_READINESS_PREWARM", True)

    def unavailable():
        raise livekit.LiveVoiceUnavailable("plugin_import_failed", "unavailable")

    def unexpected_probe():
        raise AssertionError("No background import retry after missing plugin")

    monkeypatch.setattr(livekit, "import_live_model", unavailable)
    monkeypatch.setattr(
        livekit,
        "_live_voice_readiness_refresher",
        SimpleNamespace(start=unexpected_probe),
    )
    proc = SimpleNamespace(userdata={})
    livekit.prewarm(proc)
    assert proc.userdata["vad"] is not None


def test_job_received_logs_the_dispatch_latency(caplog):
    import logging
    import time

    caplog.set_level(logging.INFO, logger=livekit.logger.name)
    ctx = _fake_ctx()
    ctx.job.room.creation_time_ms = int(time.time() * 1000) - 1500
    livekit._log_job_received(ctx)
    (record,) = [
        r for r in caplog.records if "stage=agent_job_received" in r.getMessage()
    ]
    message = record.getMessage()
    assert "job_id=AJ_1" in message and "dispatch_id=AD_1" in message
    age = int(message.split("room_age_ms=")[1].split()[0])
    assert 1400 <= age <= 5000


def test_live_model_uses_dispatcher_mapping_and_explicit_agent_voice(monkeypatch):
    from openbase_coder_cli import voice_identity

    monkeypatch.setattr(livekit, "import_live_model", lambda: _FakeGPTLiveModel)
    monkeypatch.setattr(
        voice_identity,
        "current_voice_identity",
        lambda: SimpleNamespace(gpt_live_voice="beacon"),
    )
    assert livekit._build_live_voice_model(_live_decision()).kwargs["voice"] == "beacon"
    assert (
        livekit._build_live_voice_model(_live_decision(), voice="cedar").kwargs["voice"]
        == "cedar"
    )
