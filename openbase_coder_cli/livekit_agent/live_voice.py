"""Live Voice engine start-up: Cloud credentials, pre-start checks, fail-soft.

The Live Voice engine (``dev-docs/live-voice.md``) runs the call on OpenAI
GPT-Live with client delegation to Super Agent threads. GPT-Live is always
reached through the Openbase Cloud live voice gateway under the
installation's Cloud token: an OpenAI key never lives on a user's device.
The engine is the default voice model, but the gateway may not be deployed
yet when an install updates, and the install may have no Cloud login. This
module decides, before the ``AgentSession`` starts, whether the live engine
can serve the call. When it cannot, the call falls back to the pipeline
engine with the user's STT/TTS providers, one warning is logged, and clients
get a non-fatal ``live_voice_unavailable`` status packet instead of a dead
call. Only a pipeline that also fails to start ends the call with the
existing fatal codes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp

from openbase_coder_cli.config.cloud_audio import (
    OpenbaseCloudAudioSubscriptionError,
    OpenbaseCloudLiveVoiceUnavailableError,
    ensure_openbase_cloud_audio_subscription,
)
from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
)
from openbase_coder_cli.livekit_agent.config import (
    LIVE_VOICE_PREFLIGHT_CLOSE_WAIT_SECONDS,
    LIVE_VOICE_PREFLIGHT_TIMEOUT_SECONDS,
    LIVE_VOICE_READINESS_REFRESH_SECONDS,
    LIVE_VOICE_READINESS_RETRY_SECONDS,
    LIVE_VOICE_READINESS_TTL_SECONDS,
    OPENBASE_CLOUD_LIVE_BASE_URL,
    WEB_BACKEND_URL,
)
from openbase_coder_cli.livekit_agent.logging_utils import exception_chain_summary
from openbase_coder_cli.voice_models import (
    VOICE_ENGINE_LIVE,
    VOICE_ENGINE_PIPELINE,
)

logger = logging.getLogger(__name__)

LIVE_VOICE_PROVIDER_ID = "openbase_cloud"

# Agent status packet codes (``openbase.agent.status``). ``live_voice_unavailable``
# is informational: the call continues on the pipeline engine.
LIVE_VOICE_UNAVAILABLE_CODE = "live_voice_unavailable"
LIVE_VOICE_PROVIDER_FAILED_CODE = "live_voice_provider_failed"

# Gateway close codes (api/docs/live-voice-gateway.md): login / entitlement.
GATEWAY_CLOSE_LOGIN_REQUIRED = 4401
GATEWAY_CLOSE_SUBSCRIPTION_REQUIRED = 4403


class LiveVoiceUnavailable(RuntimeError):
    """The live engine cannot start this call; fall back to the pipeline."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class LiveVoiceSessionError(RuntimeError):
    """An unrecoverable GPT-Live session error after the call started."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(exception_chain_summary(error))
        self.__cause__ = error


@dataclass(frozen=True)
class LiveVoiceCredentials:
    base_url: str
    api_key: str
    provider_id: str = LIVE_VOICE_PROVIDER_ID

    @property
    def sessions_url(self) -> str:
        """The websocket URL the GPT-Live plugin derives from ``base_url``."""
        parsed = urlparse(self.base_url)
        scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
        path = parsed.path.rstrip("/")
        if not path.endswith("/live/sessions"):
            path = f"{path}/live/sessions"
        return urlunparse((scheme, parsed.netloc, path, "", "", ""))


READINESS_PROBED = "probed"
READINESS_CACHED = "cached"


@dataclass(frozen=True)
class VoiceEngineDecision:
    engine: str
    credentials: LiveVoiceCredentials | None = None
    fallback_reason: str | None = None
    fallback_detail: str | None = None
    # How the live engine's prerequisites were established for this call:
    # ``probed`` (entitlement + gateway handshake ran now) or ``cached`` (a
    # fresh result from the readiness cache, nothing was waited on).
    readiness: str = READINESS_PROBED

    @property
    def is_live(self) -> bool:
        return self.engine == VOICE_ENGINE_LIVE

    @property
    def fell_back(self) -> bool:
        return self.fallback_reason is not None


def resolve_live_voice_credentials(
    *,
    cloud_token_provider: Callable[[], str],
    base_url: str | None = None,
) -> LiveVoiceCredentials:
    """The gateway endpoint and the installation's Cloud token for the session.

    ``cloud_token_provider`` is the same machine-token lookup the Cloud STT/TTS
    proxies use; no login means no live engine (the pipeline may still run on
    direct keys or local audio). ``base_url`` defaults to
    ``OPENBASE_CLOUD_LIVE_BASE_URL`` (env-overridable for staging or a local
    cloud API), read at call time.
    """
    if base_url is None:
        base_url = OPENBASE_CLOUD_LIVE_BASE_URL
    try:
        token = cloud_token_provider()
    except (AuthLoginRequiredError, AuthTransientError) as exc:
        raise LiveVoiceUnavailable("login_required", str(exc)) from exc
    except Exception as exc:  # OpenbaseCloudAudioAuthenticationError and kin
        raise LiveVoiceUnavailable("login_required", str(exc)) from exc
    if not token:
        raise LiveVoiceUnavailable(
            "login_required",
            "Live voice needs a valid Openbase machine token. Run "
            "`openbase-coder login` and restart the Openbase services.",
        )
    return LiveVoiceCredentials(base_url=base_url.rstrip("/"), api_key=token)


def import_live_model() -> Any:
    """The GPT-Live model class, or ``LiveVoiceUnavailable`` when missing."""
    try:
        from livekit.plugins.openai.realtime import GPTLiveModel
    except Exception as exc:  # ImportError, or a plugin-internal import failure
        raise LiveVoiceUnavailable(
            "plugin_import_failed",
            "The OpenAI GPT-Live plugin could not be imported: "
            f"{exception_chain_summary(exc)}.",
        ) from exc
    return GPTLiveModel


def check_live_voice_entitlement(
    credentials: LiveVoiceCredentials,
    *,
    tts_provider_id: str,
    stt_provider_id: str,
    web_backend_url: str = WEB_BACKEND_URL,
) -> None:
    """Cloud entitlement for the ``live_voice`` provider (sync; run in a thread)."""
    try:
        ensure_openbase_cloud_audio_subscription(
            tts_provider_id=tts_provider_id,
            stt_provider_id=stt_provider_id,
            web_backend_url=web_backend_url,
            live_voice=True,
        )
    except OpenbaseCloudLiveVoiceUnavailableError as exc:
        raise LiveVoiceUnavailable("cloud_live_voice_unknown", str(exc)) from exc
    except OpenbaseCloudAudioSubscriptionError as exc:
        raise LiveVoiceUnavailable("subscription_required", str(exc)) from exc
    except AuthLoginRequiredError as exc:
        raise LiveVoiceUnavailable("login_required", str(exc)) from exc
    except AuthTransientError as exc:
        # Same policy as the pipeline's subscription check: a transient Cloud
        # hiccup must not block the call. The handshake probe still runs.
        logger.warning("Skipping the live voice entitlement check this call: %s", exc)


async def preflight_live_voice(
    credentials: LiveVoiceCredentials,
    *,
    http_session: aiohttp.ClientSession | None = None,
    timeout: float = LIVE_VOICE_PREFLIGHT_TIMEOUT_SECONDS,
    close_wait: float = LIVE_VOICE_PREFLIGHT_CLOSE_WAIT_SECONDS,
) -> None:
    """Handshake with the live sessions endpoint without starting a session.

    Nothing is billed before ``session.start``; the probe only opens the
    websocket with the bearer the plugin will use, waits briefly for an
    immediate close (the gateway rejects with 4401/4403 after accepting the
    handshake) and closes. A refused connection, an HTTP 401/403/404
    handshake, or those close codes raise ``LiveVoiceUnavailable``.
    """
    owned_session = http_session is None
    session = http_session or aiohttp.ClientSession()
    url = credentials.sessions_url
    headers = {
        "Authorization": f"Bearer {credentials.api_key}",
        "User-Agent": "Openbase Coder live voice preflight",
    }
    try:
        try:
            ws = await asyncio.wait_for(
                session.ws_connect(url, headers=headers), timeout
            )
        except aiohttp.WSServerHandshakeError as exc:
            status = int(getattr(exc, "status", 0) or 0)
            raise LiveVoiceUnavailable(
                _handshake_reason(status),
                f"The live voice endpoint {_display_url(url)} answered the "
                f"websocket handshake with HTTP {status or 'error'}.",
            ) from None
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            raise LiveVoiceUnavailable(
                "gateway_unreachable",
                f"The live voice endpoint {_display_url(url)} could not be "
                f"reached: {exception_chain_summary(exc) or type(exc).__name__}.",
            ) from None
        try:
            try:
                message = await asyncio.wait_for(ws.receive(), close_wait)
            except asyncio.TimeoutError:
                return
            if message.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            ):
                code = int(ws.close_code or message.data or 0)
                raise LiveVoiceUnavailable(
                    _close_reason(code),
                    f"The live voice endpoint {_display_url(url)} closed the "
                    f"connection immediately with code {code}.",
                )
            if message.type == aiohttp.WSMsgType.ERROR:
                raise LiveVoiceUnavailable(
                    "gateway_error",
                    f"The live voice endpoint {_display_url(url)} failed right "
                    "after the handshake.",
                )
        finally:
            try:
                await ws.close()
            except Exception:
                logger.debug("live voice preflight close failed", exc_info=True)
    finally:
        if owned_session:
            await session.close()


def _handshake_reason(status: int) -> str:
    if status == 401:
        return "gateway_http_401"
    if status == 403:
        return "gateway_http_403"
    if status == 404:
        return "gateway_not_deployed"
    return f"gateway_http_{status or 'error'}"


def _close_reason(code: int) -> str:
    if code == GATEWAY_CLOSE_LOGIN_REQUIRED:
        return "gateway_close_4401"
    if code == GATEWAY_CLOSE_SUBSCRIPTION_REQUIRED:
        return "gateway_close_4403"
    return f"gateway_closed_{code}"


def _display_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


@dataclass(frozen=True)
class LiveVoiceReadiness:
    """One successful entitlement + handshake check, keyed by what it proved."""

    credentials: LiveVoiceCredentials
    tts_provider_id: str
    stt_provider_id: str
    checked_at: float

    def matches(
        self,
        credentials: LiveVoiceCredentials,
        *,
        tts_provider_id: str,
        stt_provider_id: str,
    ) -> bool:
        return (
            self.credentials == credentials
            and self.tts_provider_id == tts_provider_id
            and self.stt_provider_id == stt_provider_id
        )


class LiveVoiceReadinessCache:
    """Remembers that the live engine's prerequisites held, for a while.

    The entitlement call and the gateway handshake probe (which waits
    ``LIVE_VOICE_PREFLIGHT_CLOSE_WAIT_SECONDS`` for a rejection) cost about a
    second per call on a cloud workspace (forensics F6, 2026-10-09). A result
    is reused for ``ttl`` seconds as long as the gateway URL, the Cloud token
    and the providers are unchanged; only successes are stored, so a failure
    is always re-probed on the next call and a recovered gateway is picked
    up immediately. Thread-safe: the refresher thread writes while job
    processes read.
    """

    def __init__(self, ttl: float = LIVE_VOICE_READINESS_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._readiness: LiveVoiceReadiness | None = None

    def get(
        self,
        credentials: LiveVoiceCredentials,
        *,
        tts_provider_id: str,
        stt_provider_id: str,
        now: float | None = None,
    ) -> LiveVoiceReadiness | None:
        """The fresh, matching readiness or None."""
        with self._lock:
            readiness = self._readiness
        if readiness is None or not readiness.matches(
            credentials,
            tts_provider_id=tts_provider_id,
            stt_provider_id=stt_provider_id,
        ):
            return None
        age = (time.monotonic() if now is None else now) - readiness.checked_at
        if age > self.ttl:
            return None
        return readiness

    def store(
        self,
        credentials: LiveVoiceCredentials,
        *,
        tts_provider_id: str,
        stt_provider_id: str,
        now: float | None = None,
    ) -> LiveVoiceReadiness:
        readiness = LiveVoiceReadiness(
            credentials=credentials,
            tts_provider_id=tts_provider_id,
            stt_provider_id=stt_provider_id,
            checked_at=time.monotonic() if now is None else now,
        )
        with self._lock:
            self._readiness = readiness
        return readiness

    def clear(self) -> None:
        with self._lock:
            self._readiness = None


class LiveVoiceReadinessRefresher:
    """Keeps a readiness cache warm from a daemon thread.

    Runs ``probe`` (a sync callable that performs the full check and stores a
    success in the cache) right away, then every ``interval`` seconds, or
    ``retry_interval`` seconds after a probe that failed, until ``stop`` is
    called. Meant for the idle agent job process: ``prewarm`` starts it, so
    the call that lands on the process finds a fresh result instead of
    paying the probe, and the entrypoint stops it so no probe runs during a
    call. Probe errors are logged, never raised.
    """

    def __init__(
        self,
        probe: Callable[[], bool],
        *,
        interval: float = LIVE_VOICE_READINESS_REFRESH_SECONDS,
        retry_interval: float = LIVE_VOICE_READINESS_RETRY_SECONDS,
    ) -> None:
        self._probe = probe
        self._interval = interval
        self._retry_interval = retry_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.probes = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="live-voice-readiness", daemon=True
        )
        self._thread.start()

    def stop(self, *, join_timeout: float | None = None) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and join_timeout is not None:
            thread.join(join_timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ok = bool(self._probe())
            except Exception:
                logger.warning("Live voice readiness probe crashed", exc_info=True)
                ok = False
            self.probes += 1
            wait = self._interval if ok else self._retry_interval
            if self._stop.wait(wait):
                return


def probe_live_voice_readiness(
    cache: LiveVoiceReadinessCache,
    *,
    selected_engine: str,
    tts_provider_id: str,
    stt_provider_id: str,
    cloud_token_provider: Callable[[], str],
) -> bool:
    """Run the live engine's checks now and store a success in ``cache``.

    Sync (runs on its own event loop) so a daemon thread can call it. Returns
    False, without storing anything, when the live engine is not selected or
    any check fails; the reason is logged once at info level since a probe
    failure in the background is expected whenever the install has no live
    voice, and the per-call decision reports its own fallback.
    """
    if selected_engine != VOICE_ENGINE_LIVE:
        return False
    started = time.monotonic()

    async def _run() -> VoiceEngineDecision:
        # The checks are named here (not left to the defaults) so they are
        # the module's current functions at call time.
        return await decide_voice_engine(
            selected_engine=selected_engine,
            tts_provider_id=tts_provider_id,
            stt_provider_id=stt_provider_id,
            cloud_token_provider=cloud_token_provider,
            import_model=import_live_model,
            check_entitlement=check_live_voice_entitlement,
            preflight=preflight_live_voice,
            readiness_cache=cache,
            use_cached_readiness=False,
        )

    decision = asyncio.run(_run())
    logger.info(
        "dispatch_timing stage=live_voice_readiness_probe engine=%s reason=%s "
        "elapsed_ms=%d",
        decision.engine,
        decision.fallback_reason or "",
        int((time.monotonic() - started) * 1000),
    )
    return decision.is_live


async def decide_voice_engine(
    *,
    selected_engine: str,
    tts_provider_id: str,
    stt_provider_id: str,
    cloud_token_provider: Callable[[], str],
    import_model: Callable[[], Any] = import_live_model,
    resolve_credentials: Callable[..., LiveVoiceCredentials] = (
        resolve_live_voice_credentials
    ),
    check_entitlement: Callable[..., None] = check_live_voice_entitlement,
    preflight: Callable[..., Any] = preflight_live_voice,
    readiness_cache: LiveVoiceReadinessCache | None = None,
    use_cached_readiness: bool = True,
) -> VoiceEngineDecision:
    """Pick the engine for this call, falling back to the pipeline when live cannot start.

    With a ``readiness_cache``, a fresh matching entry stands in for the
    entitlement check and the handshake probe (``readiness == "cached"``), and
    a probe that passes is stored for the next call.
    """
    if selected_engine != VOICE_ENGINE_LIVE:
        return VoiceEngineDecision(engine=VOICE_ENGINE_PIPELINE)
    try:
        import_model()
        credentials = resolve_credentials(cloud_token_provider=cloud_token_provider)
        if (
            readiness_cache is not None
            and use_cached_readiness
            and readiness_cache.get(
                credentials,
                tts_provider_id=tts_provider_id,
                stt_provider_id=stt_provider_id,
            )
            is not None
        ):
            return VoiceEngineDecision(
                engine=VOICE_ENGINE_LIVE,
                credentials=credentials,
                readiness=READINESS_CACHED,
            )
        await asyncio.to_thread(
            check_entitlement,
            credentials,
            tts_provider_id=tts_provider_id,
            stt_provider_id=stt_provider_id,
        )
        await preflight(credentials)
    except LiveVoiceUnavailable as exc:
        return _fallback(exc.reason, exc.detail)
    except Exception as exc:
        return _fallback(
            "unexpected_error",
            f"Unexpected error preparing live voice: {exception_chain_summary(exc)}.",
        )
    if readiness_cache is not None:
        readiness_cache.store(
            credentials,
            tts_provider_id=tts_provider_id,
            stt_provider_id=stt_provider_id,
        )
    return VoiceEngineDecision(engine=VOICE_ENGINE_LIVE, credentials=credentials)


def _fallback(reason: str, detail: str) -> VoiceEngineDecision:
    logger.warning(
        "Live voice is unavailable for this call (reason=%s); falling back to the "
        "pipeline voice engine: %s",
        reason,
        detail,
    )
    return VoiceEngineDecision(
        engine=VOICE_ENGINE_PIPELINE,
        fallback_reason=reason,
        fallback_detail=detail,
    )


def live_voice_unavailable_detail(decision: VoiceEngineDecision) -> str:
    """Human-readable packet detail for a live -> pipeline fallback."""
    return (
        "Live voice is unavailable for this call, so the classic voice pipeline "
        f"is used instead (reason: {decision.fallback_reason}). "
        f"{decision.fallback_detail or ''}"
    ).strip()
