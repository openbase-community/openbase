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


@dataclass(frozen=True)
class VoiceEngineDecision:
    engine: str
    credentials: LiveVoiceCredentials | None = None
    fallback_reason: str | None = None
    fallback_detail: str | None = None

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
) -> VoiceEngineDecision:
    """Pick the engine for this call, falling back to the pipeline when live cannot start."""
    if selected_engine != VOICE_ENGINE_LIVE:
        return VoiceEngineDecision(engine=VOICE_ENGINE_PIPELINE)
    try:
        import_model()
        credentials = resolve_credentials(cloud_token_provider=cloud_token_provider)
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
