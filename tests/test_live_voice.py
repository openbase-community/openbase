"""Live Voice start-up: Cloud credentials, entitlement, preflight and fail-soft.

Includes a fake GPT-Live websocket server (aiohttp, loopback) that speaks the
``session.start`` / ``session.started`` / transcript delta /
``session.delegation.created`` / ``*.appended`` protocol, so both the
preflight probe and the real ``GPTLiveModel`` plugin session can be exercised
without a key or network.
"""

from __future__ import annotations

import asyncio
import json
from unittest import mock

import aiohttp
import httpx
import pytest
from aiohttp import web

from openbase_coder_cli.config import cloud_audio
from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
)
from openbase_coder_cli.livekit_agent import live_voice
from openbase_coder_cli.livekit_agent.live_delegation import LiveDelegationBridge
from openbase_coder_cli.livekit_agent.live_voice import (
    LiveVoiceCredentials,
    LiveVoiceUnavailable,
    VoiceEngineDecision,
    decide_voice_engine,
    live_voice_unavailable_detail,
    preflight_live_voice,
    resolve_live_voice_credentials,
)


class FakeGPTLiveServer:
    """Loopback stand-in for the Openbase Cloud GPT-Live gateway."""

    def __init__(
        self,
        *,
        expected_token: str = "cloud-token",
        handshake_status: int | None = None,
        close_code: int | None = None,
    ) -> None:
        self.expected_token = expected_token
        self.handshake_status = handshake_status
        self.close_code = close_code
        self.connections = 0
        self.client_events: list[dict] = []
        self.session_start: dict | None = None
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self._ws: web.WebSocketResponse | None = None
        self._runner: web.AppRunner | None = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    async def __aenter__(self) -> FakeGPTLiveServer:
        app = web.Application()
        app.router.add_get("/v1/live/sessions", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request):
        self.connections += 1
        if self.handshake_status is not None:
            return web.Response(status=self.handshake_status)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if self.close_code is not None:
            await ws.close(code=self.close_code)
            return ws
        if request.headers.get("Authorization") != f"Bearer {self.expected_token}":
            await ws.close(code=4401)
            return ws
        self._ws = ws
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                event = json.loads(message.data)
                if event.get("type") == "session.input_audio.append":
                    continue
                self.client_events.append(event)
                kind = event.get("type", "")
                if kind == "session.start":
                    self.session_start = event
                    await ws.send_json(
                        {"type": "session.started", "session": {"id": "sess_1"}}
                    )
                    self.started.set()
                elif kind.endswith(".append"):
                    await ws.send_json(
                        {
                            "type": kind[: -len(".append")] + ".appended",
                            "client_event_id": event.get("event_id"),
                        }
                    )
                elif kind == "session.close":
                    await ws.send_json(
                        {
                            "type": "session.closed",
                            "reason": "client",
                            "usage": {"seconds": 3},
                        }
                    )
                    break
        finally:
            self.closed.set()
        return ws

    async def send(self, event: dict) -> None:
        assert self._ws is not None
        await self._ws.send_json(event)

    def appends(self, kind: str) -> list[dict]:
        return [
            e for e in self.client_events if e.get("type") == f"session.{kind}.append"
        ]

    async def wait_for_append(self, kind: str, count: int = 1, timeout: float = 5.0):
        async def _wait():
            while len(self.appends(kind)) < count:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_wait(), timeout)
        return self.appends(kind)


def _credentials(server: FakeGPTLiveServer, token: str = "cloud-token"):
    return LiveVoiceCredentials(base_url=server.base_url, api_key=token)


# --- credentials ----------------------------------------------------------------


def test_credentials_come_from_the_cloud_token_and_gateway_url(monkeypatch):
    monkeypatch.setattr(
        live_voice,
        "OPENBASE_CLOUD_LIVE_BASE_URL",
        "https://cloud.example/api/openbase/live/openai/v1",
    )
    credentials = resolve_live_voice_credentials(cloud_token_provider=lambda: "obmt_x")
    assert credentials.api_key == "obmt_x"
    assert credentials.base_url == "https://cloud.example/api/openbase/live/openai/v1"
    assert (
        credentials.sessions_url
        == "wss://cloud.example/api/openbase/live/openai/v1/live/sessions"
    )


def test_credentials_honor_the_base_url_override_for_staging():
    credentials = resolve_live_voice_credentials(
        cloud_token_provider=lambda: "t",
        base_url="http://127.0.0.1:8000/api/openbase/live/openai/v1/",
    )
    assert credentials.sessions_url == (
        "ws://127.0.0.1:8000/api/openbase/live/openai/v1/live/sessions"
    )


@pytest.mark.parametrize(
    "failure",
    [
        AuthLoginRequiredError("not logged in"),
        AuthTransientError("cloud hiccup"),
        RuntimeError("machine token missing"),
    ],
)
def test_missing_cloud_login_is_login_required(failure):
    def provider():
        raise failure

    with pytest.raises(LiveVoiceUnavailable) as exc_info:
        resolve_live_voice_credentials(cloud_token_provider=provider)
    assert exc_info.value.reason == "login_required"

    with pytest.raises(LiveVoiceUnavailable) as exc_info:
        resolve_live_voice_credentials(cloud_token_provider=lambda: "")
    assert exc_info.value.reason == "login_required"


def test_live_engine_never_reads_an_openai_key():
    source = open(live_voice.__file__, encoding="utf-8").read()
    assert "OPENAI_API_KEY" not in source
    assert "api.openai.com" not in source


# --- entitlement -------------------------------------------------------------------


class FakeTokenManager:
    def get_access_token(self) -> str:
        return "jwt"


def _usage(**payload) -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        request=httpx.Request(
            "GET", "https://backend.example/api/openbase/audio/usage/"
        ),
    )


def test_entitlement_requires_live_voice_fields_from_the_gateway(monkeypatch):
    monkeypatch.setattr(
        cloud_audio, "get_token_manager", lambda url: FakeTokenManager()
    )
    assert cloud_audio._required_cloud_audio_providers(
        tts_provider_id="cartesia", stt_provider_id="assemblyai", live_voice=True
    ) == {"live_voice"}
    with mock.patch.object(
        httpx,
        "get",
        return_value=_usage(monthly_limit_cents=500, cartesia_remaining_cents=500),
    ):
        with pytest.raises(cloud_audio.OpenbaseCloudLiveVoiceUnavailableError):
            cloud_audio.ensure_openbase_cloud_audio_subscription(
                tts_provider_id="cartesia",
                stt_provider_id="assemblyai",
                web_backend_url="https://backend.example",
                live_voice=True,
            )


def test_entitlement_passes_with_live_voice_credits(monkeypatch):
    monkeypatch.setattr(
        cloud_audio, "get_token_manager", lambda url: FakeTokenManager()
    )
    with mock.patch.object(
        httpx,
        "get",
        return_value=_usage(
            monthly_limit_cents=500,
            live_voice_limit_cents=200,
            live_voice_remaining_cents=120,
        ),
    ):
        cloud_audio.ensure_openbase_cloud_audio_subscription(
            tts_provider_id="cartesia",
            stt_provider_id="assemblyai",
            web_backend_url="https://backend.example",
            live_voice=True,
        )


def test_live_voice_entitlement_ignores_pipeline_provider_balances(monkeypatch):
    monkeypatch.setattr(
        cloud_audio, "get_token_manager", lambda url: FakeTokenManager()
    )
    assert cloud_audio._required_cloud_audio_providers(
        tts_provider_id="openbase_cloud",
        stt_provider_id="openbase_cloud",
        live_voice=True,
    ) == {"live_voice"}
    with mock.patch.object(
        httpx,
        "get",
        return_value=_usage(
            monthly_limit_cents=500,
            cartesia_remaining_cents=0,
            assemblyai_remaining_cents=0,
            live_voice_limit_cents=200,
            live_voice_remaining_cents=120,
        ),
    ):
        cloud_audio.ensure_openbase_cloud_audio_subscription(
            tts_provider_id="openbase_cloud",
            stt_provider_id="openbase_cloud",
            web_backend_url="https://backend.example",
            live_voice=True,
        )


def test_entitlement_rejects_exhausted_live_voice_credits(monkeypatch):
    monkeypatch.setattr(
        cloud_audio, "get_token_manager", lambda url: FakeTokenManager()
    )
    with mock.patch.object(
        httpx,
        "get",
        return_value=_usage(
            monthly_limit_cents=500,
            live_voice_limit_cents=200,
            live_voice_remaining_cents=0,
        ),
    ):
        with pytest.raises(cloud_audio.OpenbaseCloudAudioSubscriptionError) as exc_info:
            cloud_audio.ensure_openbase_cloud_audio_subscription(
                tts_provider_id="cartesia",
                stt_provider_id="assemblyai",
                web_backend_url="https://backend.example",
                live_voice=True,
            )
    assert "live voice" in str(exc_info.value)


def test_entitlement_without_live_voice_is_unchanged(monkeypatch):
    monkeypatch.setattr(
        cloud_audio,
        "get_token_manager",
        lambda url: pytest.fail("direct providers never call the cloud"),
    )
    cloud_audio.ensure_openbase_cloud_audio_subscription(
        tts_provider_id="cartesia",
        stt_provider_id="assemblyai",
        web_backend_url="https://backend.example",
    )


def test_live_entitlement_check_maps_errors_to_fallback_reasons(monkeypatch):
    credentials = LiveVoiceCredentials(base_url="https://c.example/v1", api_key="t")
    for error, reason in (
        (
            cloud_audio.OpenbaseCloudLiveVoiceUnavailableError("no fields"),
            "cloud_live_voice_unknown",
        ),
        (
            cloud_audio.OpenbaseCloudAudioSubscriptionError("out of credits"),
            "subscription_required",
        ),
        (AuthLoginRequiredError("login"), "login_required"),
    ):

        def raise_error(*, error=error, **_kwargs):
            raise error

        monkeypatch.setattr(
            live_voice, "ensure_openbase_cloud_audio_subscription", raise_error
        )
        with pytest.raises(LiveVoiceUnavailable) as exc_info:
            live_voice.check_live_voice_entitlement(
                credentials, tts_provider_id="cartesia", stt_provider_id="assemblyai"
            )
        assert exc_info.value.reason == reason

    def raise_transient(**_kwargs):
        raise AuthTransientError("blip")

    monkeypatch.setattr(
        live_voice, "ensure_openbase_cloud_audio_subscription", raise_transient
    )
    live_voice.check_live_voice_entitlement(
        credentials, tts_provider_id="cartesia", stt_provider_id="assemblyai"
    )


# --- preflight probe against the fake gateway ----------------------------------------


async def test_preflight_passes_against_a_healthy_gateway_without_starting_a_session():
    async with FakeGPTLiveServer() as server:
        await preflight_live_voice(_credentials(server), close_wait=0.2)
        assert server.connections == 1
        assert server.session_start is None
        assert server.client_events == []


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "gateway_http_401"),
        (403, "gateway_http_403"),
        (404, "gateway_not_deployed"),
    ],
)
async def test_preflight_maps_handshake_rejections(status, reason):
    async with FakeGPTLiveServer(handshake_status=status) as server:
        with pytest.raises(LiveVoiceUnavailable) as exc_info:
            await preflight_live_voice(_credentials(server), close_wait=0.2)
    assert exc_info.value.reason == reason
    assert str(status) in exc_info.value.detail


@pytest.mark.parametrize(
    ("code", "reason"), [(4401, "gateway_close_4401"), (4403, "gateway_close_4403")]
)
async def test_preflight_maps_gateway_close_codes(code, reason):
    async with FakeGPTLiveServer(close_code=code) as server:
        with pytest.raises(LiveVoiceUnavailable) as exc_info:
            await preflight_live_voice(_credentials(server), close_wait=1.0)
    assert exc_info.value.reason == reason


async def test_preflight_rejects_a_wrong_cloud_token_like_the_gateway_does():
    async with FakeGPTLiveServer() as server:
        with pytest.raises(LiveVoiceUnavailable) as exc_info:
            await preflight_live_voice(_credentials(server, "stale"), close_wait=1.0)
    assert exc_info.value.reason == "gateway_close_4401"


async def test_preflight_reports_a_refused_connection():
    async with FakeGPTLiveServer() as server:
        port = server.port
    credentials = LiveVoiceCredentials(
        base_url=f"http://127.0.0.1:{port}/v1", api_key="t"
    )
    with pytest.raises(LiveVoiceUnavailable) as exc_info:
        await preflight_live_voice(credentials, close_wait=0.2)
    assert exc_info.value.reason == "gateway_unreachable"


# --- the decision ---------------------------------------------------------------------


async def _decide(**overrides):
    async def ok_preflight(credentials):
        return None

    kwargs = dict(
        selected_engine="live",
        tts_provider_id="cartesia",
        stt_provider_id="assemblyai",
        cloud_token_provider=lambda: "cloud-token",
        import_model=lambda: object,
        check_entitlement=lambda credentials, **_kw: None,
        preflight=ok_preflight,
    )
    kwargs.update(overrides)
    return await decide_voice_engine(**kwargs)


async def test_decision_keeps_the_pipeline_when_selected():
    decision = await _decide(
        selected_engine="pipeline", import_model=lambda: pytest.fail("x")
    )
    assert decision.engine == "pipeline" and not decision.fell_back


async def test_decision_is_live_when_every_check_passes(monkeypatch):
    monkeypatch.setattr(
        live_voice, "OPENBASE_CLOUD_LIVE_BASE_URL", "https://c.example/live/v1"
    )
    decision = await _decide()
    assert decision.is_live and not decision.fell_back
    assert decision.credentials == LiveVoiceCredentials(
        base_url="https://c.example/live/v1", api_key="cloud-token"
    )


async def test_decision_falls_back_without_a_cloud_login():
    def no_login():
        raise AuthLoginRequiredError("run openbase-coder login")

    decision = await _decide(cloud_token_provider=no_login)
    assert decision.engine == "pipeline"
    assert decision.fallback_reason == "login_required"
    assert "openbase-coder login" in live_voice_unavailable_detail(decision)


async def test_decision_falls_back_when_the_gateway_is_unknown_to_the_cloud():
    def unknown(credentials, **_kw):
        raise LiveVoiceUnavailable("cloud_live_voice_unknown", "no live_voice fields")

    decision = await _decide(check_entitlement=unknown)
    assert decision.fallback_reason == "cloud_live_voice_unknown"


async def test_decision_falls_back_when_live_credits_are_exhausted():
    def exhausted(credentials, **_kw):
        raise LiveVoiceUnavailable("subscription_required", "out of live voice credits")

    decision = await _decide(check_entitlement=exhausted)
    assert decision.engine == "pipeline"
    assert decision.fallback_reason == "subscription_required"


@pytest.mark.parametrize(
    "reason",
    [
        "gateway_unreachable",
        "gateway_close_4401",
        "gateway_close_4403",
        "gateway_not_deployed",
    ],
)
async def test_decision_falls_back_on_preflight_failures(reason):
    async def failing_preflight(credentials):
        raise LiveVoiceUnavailable(reason, "probe failed")

    decision = await _decide(preflight=failing_preflight)
    assert decision.engine == "pipeline" and decision.fallback_reason == reason


async def test_decision_falls_back_when_the_plugin_cannot_be_imported():
    def broken_import():
        raise LiveVoiceUnavailable(
            "plugin_import_failed", "no module livekit.plugins.openai"
        )

    decision = await _decide(import_model=broken_import)
    assert decision.fallback_reason == "plugin_import_failed"


async def test_decision_falls_back_on_unexpected_errors_too():
    async def exploding_preflight(credentials):
        raise ValueError("boom")

    decision = await _decide(preflight=exploding_preflight)
    assert decision.engine == "pipeline"
    assert decision.fallback_reason == "unexpected_error"
    assert "boom" in (decision.fallback_detail or "")


def test_import_live_model_returns_the_plugin_class():
    from livekit.plugins.openai.realtime import GPTLiveModel

    assert live_voice.import_live_model() is GPTLiveModel


def test_unavailable_detail_names_the_reason():
    decision = VoiceEngineDecision(
        engine="pipeline",
        fallback_reason="gateway_not_deployed",
        fallback_detail="HTTP 404.",
    )
    detail = live_voice_unavailable_detail(decision)
    assert "classic voice pipeline" in detail and "gateway_not_deployed" in detail
    assert detail.endswith("HTTP 404.")


# --- the real plugin session against the fake gateway ------------------------------------


class _FakeClient:
    _thread_id = "thread-1"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def run_turn(self, prompt, *, developer_instructions=None):
        self.prompts.append(prompt)
        self.started.set()
        await self.gate.wait()
        return {
            "_livekit_speech_text": "The build passed.",
            "_livekit_turn_id": "turn-1",
            "status": "completed",
            "progress": {},
        }

    def claim_speech(self, turn_id):
        return True


class _FakeRouter:
    def __init__(self, client):
        self.active_client = client
        self.is_dispatcher_active = True

    def route_snapshot(self):
        from openbase_coder_cli.livekit_agent.voice_delivery import VoiceRouteSnapshot

        return VoiceRouteSnapshot(0, "thread-1", None, None, "dispatcher")

    def can_deliver_for_snapshot(self, snapshot):
        return True

    def exit_to_dispatch(self):
        return False

    def claim_speech(self, client, turn_id):
        return True


async def test_real_gpt_live_session_delegation_flows_through_the_bridge():
    from livekit.agents.llm import ChatContext
    from livekit.plugins.openai.realtime import GPTLiveModel

    async with FakeGPTLiveServer() as server:
        model = GPTLiveModel(
            model="gpt-live-1",
            voice="marin",
            delegation="client",
            api_key="cloud-token",
            base_url=server.base_url,
        )
        live = model.session()
        client = _FakeClient()
        bridge = LiveDelegationBridge(
            voice_router=_FakeRouter(client),
            developer_instructions=lambda: "guidance",
            progress_thinking_interval=3600,
        )
        bridge.attach(live)
        try:
            await live._update_session(
                instructions="You are the voice relay.",
                chat_ctx=ChatContext.empty(),
                tools=[],
            )
            await asyncio.wait_for(server.started.wait(), 5)
            assert server.session_start["session"]["delegation"] == {"type": "client"}
            assert server.session_start["session"]["model"] == "gpt-live-1"
            assert (
                server.session_start["session"]["instructions"]
                == "You are the voice relay."
            )

            await server.send(
                {
                    "type": "session.input_transcript.delta",
                    "delta": "Check whether the build passes",
                    "start_ms": 0,
                    "end_ms": 900,
                }
            )
            await server.send(
                {
                    "type": "session.delegation.created",
                    "event_id": "evt_1",
                    "offset_ms": 1000,
                    "delegation": {
                        "id": "item_1",
                        "type": "delegation",
                        "target": "client",
                    },
                }
            )
            await asyncio.wait_for(client.started.wait(), 5)
            assert "Check whether the build passes" in client.prompts[0]
            thinking = await server.wait_for_append("thinking")
            assert thinking[0]["delegation_id"] == "item_1"
            assert "Check whether the build passes" in thinking[0]["content"]

            client.gate.set()
            commentary = await server.wait_for_append("commentary")
            assert commentary[0]["delegation_id"] == "item_1"
            assert commentary[0]["content"] == "The build passed."

            bridge.announce("Report ready.", agent_name="Lucy")
            announcements = await server.wait_for_append("commentary", count=2)
            assert (
                announcements[1]["delegation_id"] is None
                or "delegation_id" not in announcements[1]
            )
            assert announcements[1]["content"] == "Lucy: Report ready."
        finally:
            await bridge.aclose()
            await live.aclose()
            await model.aclose()


async def _close_caller_utterance(live, *, seconds: float = 1.0) -> None:
    """Push caller audio with no new transcript until the plugin ends the turn."""
    from livekit import rtc

    for _ in range(round(seconds * 10)):
        live.push_audio(
            rtc.AudioFrame.create(
                sample_rate=24000, num_channels=1, samples_per_channel=2400
            )
        )


@pytest.mark.parametrize("model_delegates", [False, True])
async def test_real_gpt_live_session_sends_every_closed_utterance_to_the_agent(
    model_delegates,
):
    """Regression: "what's on my desktop" must reach the agent thread.

    Through the real ``GPTLiveModel`` plugin: the caller's utterance closes
    (0.8 s of audio with no new transcript) and the bridge runs the agent
    turn whether or not GPT-Live delegates. When it does delegate after the
    utterance closed, the delegation binds to the same single turn.
    """
    from livekit.agents.llm import ChatContext
    from livekit.plugins.openai.realtime import GPTLiveModel

    async with FakeGPTLiveServer() as server:
        model = GPTLiveModel(
            model="gpt-live-1",
            voice="marin",
            delegation="client",
            api_key="cloud-token",
            base_url=server.base_url,
        )
        live = model.session()
        client = _FakeClient()
        bridge = LiveDelegationBridge(
            voice_router=_FakeRouter(client),
            developer_instructions=lambda: "guidance",
            progress_thinking_interval=3600,
        )
        bridge.attach(live)
        try:
            await live._update_session(
                instructions="You are the voice relay.",
                chat_ctx=ChatContext.empty(),
                tools=[],
            )
            await asyncio.wait_for(server.started.wait(), 5)
            await server.send(
                {
                    "type": "session.input_transcript.delta",
                    "delta": "What's on my desktop?",
                    "start_ms": 0,
                    "end_ms": 900,
                }
            )
            await asyncio.sleep(0.05)
            assert client.prompts == [], "an open utterance must not start a turn"
            await _close_caller_utterance(live)
            await asyncio.wait_for(client.started.wait(), 5)
            assert len(client.prompts) == 1
            assert client.prompts[0].endswith("<voice>What's on my desktop?</voice>")

            expected_delegation = None
            if model_delegates:
                await server.send(
                    {
                        "type": "session.delegation.created",
                        "event_id": "evt_1",
                        "offset_ms": 1000,
                        "delegation": {
                            "id": "item_1",
                            "type": "delegation",
                            "target": "client",
                        },
                    }
                )
                thinking = await server.wait_for_append("thinking", count=2)
                assert thinking[1]["delegation_id"] == "item_1"
                expected_delegation = "item_1"

            client.gate.set()
            commentary = await server.wait_for_append("commentary")
            assert len(client.prompts) == 1, "exactly one agent turn"
            assert commentary[0]["content"] == "The build passed."
            assert commentary[0].get("delegation_id") == expected_delegation
        finally:
            await bridge.aclose()
            await live.aclose()
            await model.aclose()
