"""A voice call belongs to the thread it was started from.

The phone passes the open thread to ``/api/livekit-room-token/``; the token
view prepares that thread for direct voice and hands the ``transfer_to_thread``
command to the agent in the dispatch metadata; the agent applies it before the
voice session starts, on both engines. A call started from the dispatcher (or
with no thread) stays a dispatcher call, and "Back to dispatch" still works.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from openbase_coder_cli.livekit_agent import (
    config,
    live_delegation,
    livekit,
    voice_routing,
)
from openbase_coder_cli.livekit_agent.codex_llm import CodexLLMStream
from openbase_coder_cli.livekit_agent.packets import voice_route_command_from_payload
from tests.test_livekit_live_engine_wiring import (
    _fake_ctx,
    _FakeAgentSession,
    _FakeGPTLiveModel,
    _live_decision,
    _run_entrypoint,
    wiring,  # noqa: F401  (pytest fixture)
)

THREAD_ID = "s_853ec00c60a44d92b03fcbb29e407a82"
DISPATCHER_ID = "s_bdcc20b0f6a34c32b700caffb9dcd622"


def _start_route_payload(**overrides) -> dict:
    payload = {
        "action": "transfer_to_thread",
        "thread_id": THREAD_ID,
        "cwd": "/data/workspace",
        "label": "Hi are you there?",
        "agent_name": "Linda",
        "state": {
            "active_target_voice_id": "829ccd10-f8b3-43cd-b8a0-4aeaa81f3b30",
            "active_target_voice_name": "Linda",
        },
    }
    payload.update(overrides)
    return payload


# --- dispatch metadata parsing -------------------------------------------------


def test_voice_route_command_from_payload_reads_the_transfer():
    command = voice_route_command_from_payload(_start_route_payload())
    assert command is not None
    assert command.action == "transfer_to_thread"
    assert command.thread_id == THREAD_ID
    assert command.cwd == "/data/workspace"
    assert command.label == "Hi are you there?"
    assert command.active_target_voice_id == "829ccd10-f8b3-43cd-b8a0-4aeaa81f3b30"
    assert command.active_target_voice_name == "Linda"


@pytest.mark.parametrize("payload", [None, "transfer", [], {}, {"thread_id": "x"}])
def test_voice_route_command_from_payload_ignores_non_commands(payload):
    assert voice_route_command_from_payload(payload) is None


def _ctx_with_metadata(metadata) -> SimpleNamespace:
    ctx = _fake_ctx()
    ctx.job = SimpleNamespace(metadata=metadata)
    return ctx


def test_requested_start_route_reads_the_token_metadata():
    ctx = _ctx_with_metadata(
        json.dumps({"user_identity": "gabe", "voice_route": _start_route_payload()})
    )
    route = livekit.requested_start_route(ctx)
    assert route is not None and route.thread_id == THREAD_ID


@pytest.mark.parametrize(
    "metadata",
    [
        "",
        json.dumps({"user_identity": "gabe"}),
        "not json",
        json.dumps({"voice_route": {"action": "exit_to_dispatch"}}),
        json.dumps({"voice_route": _start_route_payload(cwd=None)}),
    ],
)
def test_requested_start_route_is_none_for_dispatcher_calls(metadata):
    assert livekit.requested_start_route(_ctx_with_metadata(metadata)) is None


def test_requested_start_route_tolerates_a_context_without_a_job():
    ctx = _fake_ctx()
    del ctx.job
    assert not hasattr(ctx, "job")
    assert livekit.requested_start_route(ctx) is None


# --- agent entrypoint: both engines ------------------------------------------------


class _RecordingClient:
    """A Super Agents voice client that records the turns it receives."""

    def __init__(self, thread_id: str, *, super_agent_name=None) -> None:
        self._thread_id = thread_id
        self._super_agent_name = super_agent_name
        self.prompts: list[str] = []
        self.persisted_routes: list[dict] = []
        self.prepared = 0
        self.listeners: list = []
        self.model_name = "fake"

    async def prepare(self):
        self.prepared += 1
        return self._thread_id

    async def aclose(self):
        pass

    def set_orphaned_result_handler(self, handler):
        pass

    def reset_voice_route_to_dispatcher(self, **kwargs):
        self.persisted_routes.append({"active_target_thread_id": None})

    def persist_voice_route(self, **kwargs):
        self.persisted_routes.append(kwargs)

    async def run_turn(
        self, prompt, *, developer_instructions=None, replaces_active_turn=False
    ):
        assert replaces_active_turn is False
        self.prompts.append(prompt)
        return {
            "_livekit_speech_text": f"{self._thread_id} says hi",
            "_livekit_turn_id": f"turn-{len(self.prompts)}",
            "status": "completed",
            "progress": {},
        }

    def add_turn_progress_listener(self, listener):
        self.listeners.append(listener)

    def remove_turn_progress_listener(self, listener):
        self.listeners.remove(listener)

    def claim_speech(self, turn_id):
        return True

    def release_speech_claim(self, turn_id):
        pass

    def backend_appears_busy(self):
        return False

    def has_active_prompt(self, prompt):
        return False


class _FakeTargetClient(_RecordingClient):
    """Stands in for ``SuperAgentsLiveKitClient`` built by the router."""

    instances: list["_FakeTargetClient"] = []

    def __init__(self, *, initial_thread_id, super_agent_name=None, **kwargs):
        super().__init__(initial_thread_id, super_agent_name=super_agent_name)
        self.kwargs = kwargs
        _FakeTargetClient.instances.append(self)

    def set_super_agent_name(self, name):
        self._super_agent_name = name

    def set_super_agent_agent_name(self, name):
        pass


@pytest.fixture
def thread_call(monkeypatch, wiring, tmp_path):  # noqa: F811
    """A call started from THREAD_ID: the dispatcher fake, the target fake and
    a job context whose metadata carries the prepared route."""
    _FakeTargetClient.instances.clear()
    dispatcher = _RecordingClient(DISPATCHER_ID)
    monkeypatch.setattr(livekit, "_shared_voice_backend_client", dispatcher)
    monkeypatch.setattr(voice_routing, "SuperAgentsLiveKitClient", _FakeTargetClient)
    monkeypatch.setattr(
        config,
        "DEFAULT_DIRECT_LIVEKIT_INSTRUCTIONS_PATH",
        tmp_path / "missing-direct-instructions.md",
    )
    ctx = _ctx_with_metadata(
        json.dumps({"user_identity": "gabe", "voice_route": _start_route_payload()})
    )
    routers: list = []
    original = livekit.LiveKitVoiceRouter

    def capture_router(*args, **kwargs):
        router = original(*args, **kwargs)
        routers.append(router)
        return router

    monkeypatch.setattr(livekit, "LiveKitVoiceRouter", capture_router)
    return SimpleNamespace(ctx=ctx, dispatcher=dispatcher, routers=routers)


def _pipeline_decision():
    from openbase_coder_cli.livekit_agent.live_voice import (
        VOICE_ENGINE_PIPELINE,
        VoiceEngineDecision,
    )

    return VoiceEngineDecision(engine=VOICE_ENGINE_PIPELINE)


async def _speak_on_pipeline(router, text: str) -> None:
    """Run one caller utterance through the pipeline engine's LLM bridge."""
    stream = SimpleNamespace(
        _voice_router=router,
        _buffered_input=None,
        _message_id="m-1",
        _event_ch=SimpleNamespace(closed=False, send_nowait=lambda chunk: None),
        _emit_delta=lambda text: None,
    )
    await CodexLLMStream._run_accepted_prompt(stream, text, None, None)


async def test_pipeline_call_started_from_a_thread_talks_to_that_thread(
    thread_call, monkeypatch
):
    await _run_entrypoint(thread_call.ctx, _pipeline_decision(), monkeypatch)

    (router,) = thread_call.routers
    (target,) = _FakeTargetClient.instances
    assert router.is_dispatcher_active is False
    assert router.active_client is target
    assert target.kwargs["persist_thread"] is False
    assert target.kwargs["cwd"] == "/data/workspace"
    assert target.prepared == 1
    # The route file names the thread (thread list badge, Back to dispatch).
    assert thread_call.dispatcher.persisted_routes[-1] == {
        "active_target_thread_id": THREAD_ID,
        "active_target_kind": "codex_thread",
        "active_target_label": "Hi are you there?",
        "active_target_voice_id": "829ccd10-f8b3-43cd-b8a0-4aeaa81f3b30",
        "active_target_voice_name": "Linda",
        "route_owner_id": router._route_owner_id,
    }
    assert router.active_target_voice_name == "Linda"

    # Integration: speech lands in the thread and never in the dispatcher.
    await _speak_on_pipeline(router, "Hey, can you hear me")
    await _speak_on_pipeline(router, "Are you on speakerphone")
    assert len(target.prompts) == 2
    assert "Hey, can you hear me" in target.prompts[0]
    assert thread_call.dispatcher.prompts == []
    # No dispatcher screen note either: the caller is already in the thread.
    assert "Openbase system note" not in target.prompts[0]

    # Back to dispatch still works from a thread call.
    assert router.exit_to_dispatch() is True
    await _speak_on_pipeline(router, "What is next")
    assert (
        thread_call.dispatcher.prompts
        and "What is next" in thread_call.dispatcher.prompts[0]
    )
    assert len(target.prompts) == 2


async def test_live_call_started_from_a_thread_talks_to_that_thread(
    thread_call,
    monkeypatch,
    wiring,  # noqa: F811
):
    await _run_entrypoint(thread_call.ctx, _live_decision(), monkeypatch)

    (router,) = thread_call.routers
    (target,) = _FakeTargetClient.instances
    assert router.active_client is target
    (session,) = _FakeAgentSession.instances
    agent, _room = session.started_with
    # GPT-Live is told from the start who is on the call.
    assert "Linda" in agent.instructions
    assert config.live_voice_start_route_note("Linda") in agent.instructions
    assert "Your name in this call is Linda." in agent.instructions
    assert "Speak in the first person as Linda" in agent.instructions
    assert agent._bridge.active_agent_label == "Linda"
    assert agent._bridge.starting_agent_label() == "Linda"
    assert agent._bridge._call_id == thread_call.ctx.room.name
    # Explicit greeting uses the actual starting character, without a transfer.
    assert wiring.live.appends == [("commentary", "Hi, I'm Linda.", None)]

    # Integration: the caller's first utterance goes to the thread.
    bridge = agent._bridge
    bridge._settle_seconds = bridge._transcript_lag_seconds = 0.0
    bridge._hold_max_seconds = 0.01
    utterance = SimpleNamespace(
        item_id="speech_1", transcript="Hey, can you hear me", is_final=True
    )
    for handler in list(wiring.live.handlers["input_audio_transcription_completed"]):
        handler(utterance)
    await asyncio.sleep(0.1)
    assert target.prompts and "Hey, can you hear me" in target.prompts[0]
    assert thread_call.dispatcher.prompts == []


async def test_thread_start_discards_a_dispatcher_preconnection_with_another_voice(
    thread_call, monkeypatch
):
    from openbase_coder_cli import voice_identity

    monkeypatch.setattr(
        voice_identity, "current_voice_identity",
        lambda: SimpleNamespace(gpt_live_voice="beacon"),
    )
    await _run_entrypoint(thread_call.ctx, _live_decision(), monkeypatch)
    early, routed = _FakeGPTLiveModel.instances
    assert early.kwargs["voice"] == "beacon"
    assert early.closed
    assert early.sessions[0].closed
    expected = voice_identity.agent_voice_identity(_start_route_payload()["state"]["active_target_voice_id"])
    assert routed.kwargs["voice"] == expected.gpt_live_voice
    assert routed.kwargs["voice"] != "beacon"
    for callback in thread_call.ctx.shutdown_callbacks:
        await callback()
    assert routed.closed


async def test_ending_a_thread_call_resets_the_persisted_route(
    thread_call, monkeypatch
):
    """The route file must not keep naming the thread after the call ends."""
    await _run_entrypoint(thread_call.ctx, _pipeline_decision(), monkeypatch)
    (router,) = thread_call.routers
    assert (
        thread_call.dispatcher.persisted_routes[-1]["active_target_thread_id"]
        == THREAD_ID
    )

    await router.close()

    assert thread_call.dispatcher.persisted_routes[-1] == {
        "active_target_thread_id": None
    }


async def test_ending_a_dispatcher_call_leaves_the_route_alone(wiring, monkeypatch):  # noqa: F811
    dispatcher = _RecordingClient(DISPATCHER_ID)
    router = voice_routing.LiveKitVoiceRouter(dispatcher)
    await router.close()
    assert dispatcher.persisted_routes == []


async def test_dispatcher_call_is_unchanged(wiring, monkeypatch):  # noqa: F811
    """No thread in the metadata: the call starts on the dispatcher, as before."""
    _FakeTargetClient.instances.clear()
    monkeypatch.setattr(voice_routing, "SuperAgentsLiveKitClient", _FakeTargetClient)
    dispatcher = _RecordingClient(DISPATCHER_ID)
    monkeypatch.setattr(livekit, "_shared_voice_backend_client", dispatcher)
    ctx = _ctx_with_metadata(json.dumps({"user_identity": "gabe"}))

    await _run_entrypoint(ctx, _live_decision(), monkeypatch)

    assert _FakeTargetClient.instances == []
    (session,) = _FakeAgentSession.instances
    agent, _room = session.started_with
    assert agent._bridge.active_agent_label == live_delegation.DISPATCHER_AGENT_LABEL
    assert agent._bridge.starting_agent_label() is None
    assert "This call started inside" not in agent.instructions


async def test_unreachable_thread_falls_back_to_the_dispatcher_and_says_so(
    thread_call,
    monkeypatch,
    wiring,  # noqa: F811
):
    async def failing_prepare(self):
        raise RuntimeError("thread resume refused")

    monkeypatch.setattr(_FakeTargetClient, "prepare", failing_prepare)

    await _run_entrypoint(thread_call.ctx, _live_decision(), monkeypatch)

    (router,) = thread_call.routers
    assert router.is_dispatcher_active is True
    commentary = [text for kind, text, _ in wiring.live.appends if kind == "commentary"]
    assert any(
        "could not reach Hi are you there" in text and "dispatcher" in text
        for text in commentary
    )


async def test_pipeline_fallback_speaks_through_the_session(thread_call, monkeypatch):
    said: list[str] = []

    class _SpeakingSession(_FakeAgentSession):
        def say(self, text):
            said.append(text)

    async def start_pipeline(ctx, voice_router, delivery_ledger):
        return _SpeakingSession(), "announcer-tts", ()

    async def failing_prepare(self):
        raise RuntimeError("thread resume refused")

    monkeypatch.setattr(_FakeTargetClient, "prepare", failing_prepare)
    monkeypatch.setattr(livekit, "_start_voice_session", start_pipeline)

    await _run_entrypoint(thread_call.ctx, _pipeline_decision(), monkeypatch)

    (router,) = thread_call.routers
    assert router.is_dispatcher_active is True
    assert said == [
        "I could not reach Hi are you there?, so you are talking to the dispatcher."
    ]


# --- live persona --------------------------------------------------------------


def test_live_startup_instructions_use_the_dispatcher_or_thread_character(monkeypatch):
    from openbase_coder_cli import voice_identity

    monkeypatch.setattr(
        voice_identity,
        "current_voice_identity",
        lambda: SimpleNamespace(voice_name="Jacqueline"),
    )
    plain = config.live_voice_startup_instructions("mac")
    assert "This call started inside" not in plain
    routed = config.live_voice_startup_instructions("mac", agent_label="Linda")
    assert routed.startswith(config.LIVE_VOICE_STARTUP_INSTRUCTIONS)
    assert "Your name in this call is Jacqueline." in plain
    assert "Your name in this call is Dispatcher." not in plain
    assert "Your name in this call is Linda." in routed
    assert "Your name in this call is Dispatcher." not in routed
    assert "Your name in this call is Jacqueline." not in routed
    assert "everything the caller says goes to Linda" in routed
    assert "do not mention the dispatcher" in routed
    # Still the voice, never the brain.
    assert "Never answer a question or request yourself" in routed


def test_bridge_initial_label_defaults_to_the_dispatcher():
    router = SimpleNamespace(active_client=None, is_dispatcher_active=True)
    bridge = live_delegation.LiveDelegationBridge(voice_router=router)
    assert bridge.active_agent_label == live_delegation.DISPATCHER_AGENT_LABEL
    assert bridge.starting_agent_label() is None
    routed = live_delegation.LiveDelegationBridge(
        voice_router=router, initial_agent_label="  Linda "
    )
    assert routed.active_agent_label == "Linda"
    assert routed.starting_agent_label() == "Linda"


# --- route module: prepare vs publish ----------------------------------------------


class _FakeSessionManager:
    def __init__(self):
        self.calls = []

    async def resume_thread_with_developer_instructions(
        self, thread_id, directory, instructions
    ):
        self.calls.append((thread_id, directory))


def _route_module_setup(monkeypatch, tmp_path):
    from openbase_coder_cli import livekit_voice_route as voice_route

    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    instructions = tmp_path / "VOICE_INSTRUCTIONS.md"
    instructions.write_text("direct voice instructions\n", encoding="utf-8")
    monkeypatch.setattr(
        voice_route,
        "selected_tts_provider_id",
        lambda: voice_route.CARTESIA_PROVIDER_ID,
    )
    monkeypatch.setattr(
        voice_route, "CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH", instructions
    )
    monkeypatch.setattr(
        voice_route,
        "SUPER_AGENT_VOICES",
        (
            voice_route.CartesiaVoice("voice-a", "Alice"),
            voice_route.CartesiaVoice("voice-b", "Bob"),
        ),
    )
    monkeypatch.setattr(voice_route, "SUPER_AGENT_VOICE_IDS", ("voice-a", "voice-b"))
    manager = _FakeSessionManager()
    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.session_manager.get_session_manager",
        lambda: manager,
    )
    return voice_route, manager


def test_prepare_voice_route_transfer_resumes_the_thread_and_defers_the_state_write(
    monkeypatch, tmp_path
):
    voice_route, manager = _route_module_setup(monkeypatch, tmp_path)

    transfer = asyncio.run(
        voice_route.prepare_voice_route_transfer(
            THREAD_ID, directory="/data/workspace", label="Hi are you there?"
        )
    )

    assert manager.calls == [(THREAD_ID, "/data/workspace")]
    payload = transfer.command_payload()
    assert payload["action"] == "transfer_to_thread"
    assert payload["thread_id"] == THREAD_ID
    assert payload["cwd"] == "/data/workspace"
    assert payload["label"] == "Hi are you there?"
    assert payload["state"]["active_target_thread_id"] == THREAD_ID
    assert payload["state"]["active_target_voice_id"] in {"voice-a", "voice-b"}
    # Nothing persisted until the plan is committed.
    assert voice_route.get_livekit_voice_route_state().active_target_thread_id is None
    assert voice_route.get_voice_history_entry(THREAD_ID) is None

    transfer.commit()

    assert (
        voice_route.get_livekit_voice_route_state().active_target_thread_id == THREAD_ID
    )
    history = voice_route.get_voice_history_entry(THREAD_ID)
    assert history is not None and history.source == "route_transfer"


def test_prepare_voice_route_transfer_refuses_the_dispatcher(monkeypatch, tmp_path):
    voice_route, _manager = _route_module_setup(monkeypatch, tmp_path)
    voice_route.set_dispatcher_thread_id(DISPATCHER_ID)
    with pytest.raises(voice_route.VoiceRouteBlockedError):
        asyncio.run(
            voice_route.prepare_voice_route_transfer(DISPATCHER_ID, directory="/x")
        )


# --- room-token endpoint -----------------------------------------------------------


def _setup_django():
    os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings"
    )
    import django

    django.setup()


class _TokenViewManager:
    def __init__(self, threads):
        self.threads = threads

    async def get_thread_state(self, thread_id):
        return self.threads.get(thread_id)

    async def list_threads(self):
        return list(self.threads.values())


class _FakeTransfer:
    def __init__(self, payload):
        self.payload = payload
        self.commits = 0

    def command_payload(self):
        return self.payload

    def commit(self):
        self.commits += 1


@pytest.fixture
def token_view(monkeypatch, tmp_path):
    _setup_django()
    from rest_framework.test import APIRequestFactory, force_authenticate

    from openbase_coder_cli.openbase_coder_cli_app import livekit as views
    from openbase_coder_cli.services import livekit_pool_activity

    monkeypatch.setattr(livekit_pool_activity, "_ACTIVITY_DIR", tmp_path / "activity")
    monkeypatch.setattr(
        views,
        "_livekit_client_token_credentials",
        lambda: ("client-key", "client-secret"),
    )
    monkeypatch.setattr(
        views, "ensure_openbase_cloud_audio_subscription", lambda **_: None
    )
    monkeypatch.setattr(
        views,
        "local_audio_readiness",
        lambda **_: SimpleNamespace(ready=True, detail=None),
    )
    monkeypatch.setattr(views, "livekit_agent_worker_ready", lambda: True)
    thread = SimpleNamespace(
        session_id=THREAD_ID,
        name="thread-e7cea5186b77497e834e68dbf6b9e4f1",
        title="Hi are you there? (9e407a82)",
        agent_name=None,
        preview=None,
        directory="/data/workspace",
    )
    dispatcher = SimpleNamespace(
        session_id=DISPATCHER_ID,
        name="dispatcher",
        title=None,
        agent_name=None,
        preview=None,
        directory="/data/workspace",
    )
    monkeypatch.setattr(
        views,
        "get_session_manager",
        lambda: _TokenViewManager({THREAD_ID: thread, DISPATCHER_ID: dispatcher}),
    )
    monkeypatch.setattr(
        views,
        "get_livekit_voice_route_state",
        lambda: SimpleNamespace(dispatcher_thread_id=DISPATCHER_ID),
    )
    prepared: list[tuple] = []
    transfer = _FakeTransfer(_start_route_payload())

    async def fake_prepare(thread_id, *, directory, label=None, agent_name=None):
        prepared.append((thread_id, directory, label, agent_name))
        return transfer

    monkeypatch.setattr(views, "prepare_voice_route_transfer", fake_prepare)

    def post(body):
        factory = APIRequestFactory()
        request = factory.post("/api/livekit-room-token/", body, format="json")
        user = SimpleNamespace(
            is_authenticated=True,
            email="gabe@example.com",
            pk=1,
            get_full_name=lambda: "Gabe",
        )
        force_authenticate(request, user=user, token={"email": "gabe@example.com"})
        return views.livekit_room_token(request)

    return SimpleNamespace(post=post, prepared=prepared, transfer=transfer, views=views)


def _dispatch_metadata(response) -> dict:
    import jwt

    claims = jwt.decode(response.data["token"], options={"verify_signature": False})
    (agent,) = claims["roomConfig"]["agents"]
    return json.loads(agent["metadata"])


def test_room_token_carries_the_thread_the_call_starts_from(token_view):
    response = token_view.post(
        {
            "room_name": "room-1",
            "livekit_dispatch_agent_name": "livekit-agent",
            "thread_id": THREAD_ID,
            "thread_label": "Hi are you there?",
        }
    )

    assert response.status_code == 200, response.data
    metadata = _dispatch_metadata(response)
    assert metadata["user_identity"]
    assert metadata["voice_route"] == _start_route_payload()
    (prepared,) = token_view.prepared
    assert prepared[:3] == (THREAD_ID, "/data/workspace", "Hi are you there?")
    assert token_view.transfer.commits == 0


def test_room_token_without_a_thread_is_a_dispatcher_call(token_view):
    response = token_view.post(
        {"room_name": "room-1", "livekit_dispatch_agent_name": "livekit-agent"}
    )
    assert response.status_code == 200
    assert "voice_route" not in _dispatch_metadata(response)
    assert token_view.prepared == []


def test_room_token_from_the_dispatcher_thread_is_a_dispatcher_call(token_view):
    response = token_view.post(
        {
            "room_name": "room-1",
            "livekit_dispatch_agent_name": "livekit-agent",
            "thread_id": DISPATCHER_ID,
        }
    )
    assert response.status_code == 200
    assert "voice_route" not in _dispatch_metadata(response)
    assert token_view.prepared == []


def test_room_token_refuses_an_unknown_thread_instead_of_falling_back(token_view):
    response = token_view.post(
        {
            "room_name": "room-1",
            "livekit_dispatch_agent_name": "livekit-agent",
            "thread_id": "s_missing",
        }
    )
    assert response.status_code == 404
    assert response.data["code"] == "thread_not_found"
    assert "token" not in response.data


def test_room_token_reports_a_blocked_transfer(token_view, monkeypatch):
    from openbase_coder_cli.livekit_voice_route import VoiceRouteBlockedError

    async def blocked(*args, **kwargs):
        raise VoiceRouteBlockedError("no direct voice instructions")

    monkeypatch.setattr(token_view.views, "prepare_voice_route_transfer", blocked)
    response = token_view.post(
        {
            "room_name": "room-1",
            "livekit_dispatch_agent_name": "livekit-agent",
            "thread_id": THREAD_ID,
        }
    )
    assert response.status_code == 409
    assert response.data["code"] == "voice_route_blocked"


def test_inbound_invitations_cannot_pick_a_thread(token_view):
    response = token_view.post(
        {"inbound_invitation_id": "a" * 43, "thread_id": THREAD_ID}
    )
    assert response.status_code == 400
    assert token_view.prepared == []
