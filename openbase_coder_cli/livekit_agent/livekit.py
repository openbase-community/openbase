"""LiveKit agent entrypoint: server wiring for the Openbase Coder voice session.

The per-concern implementations live in sibling modules (``config``,
``voices``, ``spoken_commands``, ``codex_llm``, ``audio_scoring``,
``audio_diagnostics``, ``tts_selection``, ``packets``, ``speech_queue``,
``voice_routing``, ``room_diagnostics``, ``session_diagnostics``). Their
public names are re-exported here for backward compatibility.
"""

import asyncio
import inspect
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    cli,
)
from livekit.agents import (
    AgentServer as LiveKitAgentServer,
)
from livekit.agents import (
    stt as livekit_stt,
)
from livekit.agents.job import DEFAULT_PARTICIPANT_KINDS
from livekit.plugins import assemblyai, cartesia, deepgram, silero  # noqa: F401

from openbase_coder_cli.brain_score import (  # noqa: F401
    brain_score_token_configured,
    brain_score_token_file,
    load_brain_score_token,
)
from openbase_coder_cli.config.cloud_audio import (
    OpenbaseCloudAudioSubscriptionError,
    ensure_openbase_cloud_audio_subscription,
)
from openbase_coder_cli.config.machine_token_manager import (
    MachineTokenError,
    MachineTokenManager,
)
from openbase_coder_cli.config.token_manager import (  # noqa: F401
    DEFAULT_WEB_BACKEND_URL,
    AuthLoginRequiredError,
    AuthTransientError,
)
from openbase_coder_cli.dispatcher_config import (
    dispatcher_service_tier,
    selected_stt_provider_id,
    selected_tts_provider_id,
    selected_voice_engine,
)
from openbase_coder_cli.livekit_agent.audio_diagnostics import (  # noqa: F401
    LoggingRecognizeStream,
    LoggingSTT,
    LoggingVAD,
    LoggingVADStream,
    _log_stt_event,
)
from openbase_coder_cli.livekit_agent.audio_scoring import (  # noqa: F401
    BrainScoreAudioScorer,
    BrainScoreRecognizeStream,
    BrainScoreSTT,
    _brain_score_enabled,
    _last_brain_score_update_at,
    _load_brain_score_token,
    _upload_brain_score_chunk,
    _write_brain_score_json,
)
from openbase_coder_cli.livekit_agent.codex_llm import (  # noqa: F401
    CodexLiveKitLLM,
    CodexLLMStream,
)
from openbase_coder_cli.livekit_agent.config import (  # noqa: F401
    AGENT_STATUS_TOPIC,
    ANNOUNCER_AUDIO_KIND,
    ANNOUNCER_MAX_QUEUE_SIZE,
    ANNOUNCER_SILENCE_GRACE_SECONDS,
    ANNOUNCER_STATE_WAIT_TIMEOUT_SECONDS,
    ANNOUNCER_TOPIC,
    BRAIN_SCORE_COOLDOWN_SECONDS,
    BRAIN_SCORE_ENABLED,
    BRAIN_SCORE_ENDPOINT,
    BRAIN_SCORE_INTERVAL_SECONDS,
    BRAIN_SCORE_LATITUDE,
    BRAIN_SCORE_LONGITUDE,
    BRAIN_SCORE_MIN_DURATION_SECONDS,
    BRAIN_SCORE_OUTPUT_PATH,
    BRAIN_SCORE_TOKEN_FILE,
    CARTESIA_ANNOUNCER_VOICE_ID,
    CARTESIA_VOICE_ID,
    CODEX_APP_SERVER_URL,
    DEFAULT_DIRECT_LIVEKIT_INSTRUCTIONS_PATH,
    DEFAULT_LIVEKIT_DISPATCHER_CONFIG_PATH,
    DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS,
    DIRECT_LIVEKIT_INSTRUCTIONS_PATH_ENV,
    DIRECT_LIVEKIT_INSTRUCTIONS_TEXT_ENV,
    DISPATCHER_BUILTIN_DEVELOPER_INSTRUCTIONS,
    LIVE_VOICE_CHARACTER_START_ATTEMPTS,
    LIVE_VOICE_CHARACTER_START_TIMEOUT_SECONDS,
    LIVE_VOICE_DEFAULT_VOICE,
    LIVE_VOICE_MODEL,
    LIVE_VOICE_PRECONNECT,
    LIVE_VOICE_PREFLIGHT_TIMEOUT_SECONDS,
    LIVE_VOICE_READINESS_PREWARM,
    LIVEKIT_AGENT_HOST,
    LIVEKIT_AGENT_LOAD_THRESHOLD_ENV,
    LIVEKIT_AGENT_NUM_IDLE_PROCESSES_ENV,
    LIVEKIT_AGENT_PORT,
    LIVEKIT_AUDIO_FRAME_LOG_EVERY,
    LIVEKIT_AUDIO_FRAME_LOG_FIRST,
    LIVEKIT_CODEX_APPROVAL_POLICY,
    LIVEKIT_CODEX_FRESH_THREAD_PER_SESSION,
    LIVEKIT_CODEX_SANDBOX,
    LIVEKIT_CODEX_THREAD_CWD,
    LIVEKIT_CODEX_THREAD_STATE_PATH,
    LIVEKIT_DISPATCH_AGENT_NAME,
    LIVEKIT_DISPATCHER_CONFIG_PATH,
    LIVEKIT_DISPATCHER_WARMUP,
    LIVEKIT_PARTICIPANT_JOIN_TIMEOUT_SECONDS,
    LIVEKIT_STT_PROVIDER,
    LIVEKIT_VERBOSE_LOGGING,
    OPENBASE_CLOUD_AUDIO_BASE_URL,
    OPENBASE_CLOUD_AUDIO_CARTESIA_VERSION,
    PROACTIVE_STEER_PROMPT_CACHE_SECONDS,
    SUPPORTED_AUDIO_EXTENSIONS,
    VOICE_ENGINE_ATTRIBUTE,
    VOICE_ROUTE_TOPIC,
    WEB_BACKEND_URL,
    _canonical_env_path,
    _load_dispatcher_developer_instructions,
    _load_openbase_env,
    _optional_float_env,
    _optional_int_env,
    _read_instruction_file,
    live_voice_greeting,
    live_voice_startup_instructions,
    load_direct_livekit_developer_instructions,
)
from openbase_coder_cli.livekit_agent.gpt_live_reconnect_patch import (
    install_gpt_live_reconnect_patch,
)
from openbase_coder_cli.livekit_agent.live_delegation import LiveDelegationBridge
from openbase_coder_cli.livekit_agent.live_preconnect import (
    _preconnecting_model_class,
    wait_live_session_started,
)
from openbase_coder_cli.livekit_agent.live_speech_gate import SpeechGatedAgent
from openbase_coder_cli.livekit_agent.live_voice import (
    LIVE_VOICE_PROVIDER_FAILED_CODE,
    LIVE_VOICE_UNAVAILABLE_CODE,
    LiveVoiceReadinessCache,
    LiveVoiceReadinessRefresher,
    LiveVoiceSessionError,
    LiveVoiceUnavailable,
    VoiceEngineDecision,
    decide_voice_engine,
    import_live_model,
    live_voice_unavailable_detail,
    probe_live_voice_readiness,
)
from openbase_coder_cli.livekit_agent.logging_utils import (  # noqa: F401
    _event_text_hash,
    _frame_duration_ms,
    _should_log_audio_frame,
    exception_chain_summary,
    redact_exception_text,
)
from openbase_coder_cli.livekit_agent.packets import (  # noqa: F401
    AnnouncerAudioMessage,
    AnnouncerMessage,
    AnnouncerQueueItem,
    QueuedAnnouncerItem,
    VoiceRouteCommand,
    _optional_packet_str,
    _packet_hash,
    _packet_json_payload,
    _packet_participant_identity,
    parse_announcer_audio_packet,
    parse_announcer_packet,
    parse_voice_route_packet,
    publish_agent_error_packet,
    publish_voice_engine_attribute,
    publish_voice_lifecycle_packet,
    voice_route_command_from_payload,
)
from openbase_coder_cli.livekit_agent.proc_pool_patch import (
    install_proc_pool_liveness_patch,
)
from openbase_coder_cli.livekit_agent.provider_recovery import voice_connect_options
from openbase_coder_cli.livekit_agent.room_diagnostics import (  # noqa: F401
    _participant_log_fields,
    _register_room_diagnostics,
    _track_log_fields,
)
from openbase_coder_cli.livekit_agent.screen_context import FocusedThreadTracker
from openbase_coder_cli.livekit_agent.session_diagnostics import (
    _register_session_diagnostics,
)
from openbase_coder_cli.livekit_agent.speech_formatter import (  # noqa: F401
    format_for_speech,
)
from openbase_coder_cli.livekit_agent.speech_queue import (  # noqa: F401
    AnnouncerSpeechQueue,
    _av_frame_to_livekit_frame,
    _decode_audio_file,
)
from openbase_coder_cli.livekit_agent.spoken_commands import (  # noqa: F401
    EXIT_TO_DISPATCH_PHRASE,
    EXIT_TO_DISPATCH_PHRASES,
    _is_exit_to_dispatch_command,
    _normalize_spoken_command,
)
from openbase_coder_cli.livekit_agent.stt_log_noise import (
    install_assemblyai_idle_noise_filter,
)
from openbase_coder_cli.livekit_agent.super_agents_client import (
    SuperAgentsLiveKitClient,
)
from openbase_coder_cli.livekit_agent.transcript_dedup import (
    FinalTranscriptDedupSTT,
)
from openbase_coder_cli.livekit_agent.tts_selection import (  # noqa: F401
    SpeechFormattingSynthesizeStream,
    VoiceSelectingCartesiaTTS,
    VoiceSelectingTTS,
)
from openbase_coder_cli.livekit_agent.turn_detection import (
    SafeMultilingualModel,
    VoiceTurnSignalTracker,
)
from openbase_coder_cli.livekit_agent.vad_backlog_patch import (
    install_vad_backlog_patch,
    set_vad_backlog_listener,
)
from openbase_coder_cli.livekit_agent.voice_delivery import VoiceDeliveryLedger
from openbase_coder_cli.livekit_agent.voice_routing import (
    LiveKitVoiceRouter,
    _transfer_voice_route,
)
from openbase_coder_cli.livekit_agent.voices import (  # noqa: F401
    SUPER_AGENT_VOICE_IDS,
    SUPER_AGENT_VOICES,
    CartesiaVoice,
    _current_super_agent_voices,
    _voices_from_ids,
    dispatcher_voice_config,
    stable_super_agent_voice,
    stable_super_agent_voice_id,
)
from openbase_coder_cli.livekit_agent.worker_watchdog import (
    install_worker_failure_watchdog,
)
from openbase_coder_cli.stt_providers import (
    ASSEMBLYAI_STT_PROVIDER_ID,
    DEEPGRAM_STT_PROVIDER_ID,
    LOCAL_MLX_WHISPER_STT_PROVIDER_ID,
    OPENBASE_CLOUD_STT_ENCODING,
    OPENBASE_CLOUD_STT_MODEL,
    OPENBASE_CLOUD_STT_PROVIDER_ID,
    OPENBASE_CLOUD_STT_SAMPLE_RATE,
    MLXWhisperSTT,
)
from openbase_coder_cli.tts_providers import (  # noqa: F401
    CARTESIA_PROVIDER_ID,
    DEFAULT_CARTESIA_ANNOUNCER_VOICE_ID,
    DEFAULT_CARTESIA_TTS_VOLUME,
    DEFAULT_CARTESIA_VOICE_ID,
    KOKORO_PROVIDER_ID,
    OPENBASE_CLOUD_TTS_PROVIDER_ID,
    get_tts_provider,
)
from openbase_coder_cli.voice_models import (
    VOICE_ENGINE_LIVE,
    VOICE_ENGINE_PIPELINE,
)

logger = logging.getLogger(__name__)

ASSEMBLY_AI_API_KEY = os.getenv("ASSEMBLY_AI_API_KEY") or os.getenv(
    "ASSEMBLYAI_API_KEY"
)
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY")


def _refresh_audio_credentials() -> None:
    """Re-read audio provider keys from the on-disk env file.

    The worker process captures these once at import. If ``.env`` is written
    *after* the worker starts (e.g. a key or cloud token added during setup),
    the long-running process otherwise keeps a stale environment and every job
    crash-loops (e.g. ``Cartesia API key is required``). Refreshing per job lets
    it recover without a manual service restart."""
    global ASSEMBLY_AI_API_KEY, DEEPGRAM_API_KEY, CARTESIA_API_KEY
    _load_openbase_env(override=True)
    ASSEMBLY_AI_API_KEY = os.getenv("ASSEMBLY_AI_API_KEY") or os.getenv(
        "ASSEMBLYAI_API_KEY"
    )
    DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
    CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY")


class OpenbaseCloudAudioAuthenticationError(RuntimeError):
    """Openbase Cloud audio requires a valid Openbase machine token."""


def _livekit_agent_server_options() -> dict[str, float | int]:
    options: dict[str, float | int] = {}

    # This worker serves one user's calls on their own computer or workspace,
    # so it must take every call regardless of how busy the machine is.
    # livekit-agents' production default (0.7 CPU) made a busy Mac decline
    # its own user's call: LiveKit answered "no servers available" and the
    # phone sat on "Waiting for agent" (field test 2026-10-09, a CPU-bound
    # desktop rejected a call outright). The env override stays for a
    # deliberately shared deployment.
    load_threshold = _optional_float_env(LIVEKIT_AGENT_LOAD_THRESHOLD_ENV)
    options["load_threshold"] = (
        load_threshold if load_threshold is not None else float("inf")
    )

    # livekit-agents defaults num_idle_processes to the CPU count, which on a
    # 16-core machine prewarms 16 job processes (each holding a VAD model,
    # ~130 MB apiece, ~2.5 GB total) for a single-user agent that services one
    # or two calls at a time. Always keep the pool at one idle process unless
    # the env override asks for more.
    num_idle_processes = _optional_int_env(LIVEKIT_AGENT_NUM_IDLE_PROCESSES_ENV)
    if num_idle_processes is not None:
        options["num_idle_processes"] = num_idle_processes
    else:
        options["num_idle_processes"] = 1

    return options


class Assistant(Agent):
    """The LiveKit agent"""

    def __init__(self) -> None:
        super().__init__(
            instructions="",  # Instructions are not used due to LastMessageOnlyStream.
        )


server = LiveKitAgentServer(
    host=LIVEKIT_AGENT_HOST,
    port=LIVEKIT_AGENT_PORT,
    **_livekit_agent_server_options(),
)


def prewarm(proc: JobProcess):
    install_vad_backlog_patch()
    install_gpt_live_reconnect_patch()
    vad_model = silero.VAD.load()
    proc.userdata["vad"] = (
        LoggingVAD(vad_model) if LIVEKIT_VERBOSE_LOGGING else vad_model
    )
    if LIVE_VOICE_READINESS_PREWARM:
        # LiveKit plugins register on import and require the process main
        # thread. Prime the lazy OpenAI import before the readiness thread
        # runs; otherwise every idle probe fails before checking the gateway.
        try:
            import_live_model()
        except LiveVoiceUnavailable as exc:
            # Keep the classic pipeline usable when the optional live path
            # cannot load. The per-call decision still reports the failure.
            logger.info("Live voice prewarm unavailable: %s", exc)
            return
        _live_voice_readiness_refresher.start()


server.setup_fnc = prewarm

# Live-engine readiness (entitlement + gateway handshake) proven ahead of the
# call. The job process is prewarmed by the pool, so the refresher runs in it
# while it idles and the call it eventually serves skips the ~1 s probe; the
# entrypoint stops the refresher so nothing probes during the call.
_live_voice_readiness_cache = LiveVoiceReadinessCache()


def _probe_live_voice_readiness() -> bool:
    _refresh_audio_credentials()
    return probe_live_voice_readiness(
        _live_voice_readiness_cache,
        selected_engine=selected_voice_engine(),
        tts_provider_id=selected_tts_provider_id(),
        stt_provider_id=selected_stt_provider_id(),
        cloud_token_provider=_openbase_cloud_audio_token,
    )


_live_voice_readiness_refresher = LiveVoiceReadinessRefresher(
    _probe_live_voice_readiness
)


def _build_voice_backend_client(*, persist_thread: bool) -> SuperAgentsLiveKitClient:
    return SuperAgentsLiveKitClient(
        cwd=LIVEKIT_CODEX_THREAD_CWD,
        state_path=LIVEKIT_CODEX_THREAD_STATE_PATH,
        developer_instructions=_load_dispatcher_developer_instructions(),
        approval_policy=LIVEKIT_CODEX_APPROVAL_POLICY,
        sandbox=LIVEKIT_CODEX_SANDBOX,
        service_tier=dispatcher_service_tier(Path(LIVEKIT_DISPATCHER_CONFIG_PATH)),
        persist_thread=persist_thread,
    )


_shared_voice_backend_client = _build_voice_backend_client(persist_thread=True)


def _build_stt(vad_model=None):
    stt_provider = selected_stt_provider_id()
    if stt_provider == DEEPGRAM_STT_PROVIDER_ID:
        logger.info("Using Deepgram STT")
        stt = deepgram.STT(api_key=DEEPGRAM_API_KEY)
    elif stt_provider == ASSEMBLYAI_STT_PROVIDER_ID:
        logger.info("Using AssemblyAI STT")
        # Explicit format_turns so the plugin emits exactly one (formatted)
        # final transcript per turn instead of an unformatted/formatted pair,
        # each of which would spawn its own LLM generation.
        stt = assemblyai.STT(api_key=ASSEMBLY_AI_API_KEY, format_turns=True)
    elif stt_provider == OPENBASE_CLOUD_STT_PROVIDER_ID:
        logger.info("Using Openbase Cloud STT")
        # Pinned explicitly: composer dictation on the phones opens the same
        # proxy session with these values (dev-docs/dictation.md).
        stt = assemblyai.STT(
            api_key=_openbase_cloud_audio_token(),
            base_url=_openbase_cloud_audio_ws_base_url("assemblyai"),
            model=OPENBASE_CLOUD_STT_MODEL,
            sample_rate=OPENBASE_CLOUD_STT_SAMPLE_RATE,
            encoding=OPENBASE_CLOUD_STT_ENCODING,
            format_turns=True,
        )
    elif stt_provider == LOCAL_MLX_WHISPER_STT_PROVIDER_ID:
        logger.info("Using local MLX Whisper STT")
        vad = vad_model or silero.VAD.load()
        stt = livekit_stt.StreamAdapter(stt=MLXWhisperSTT(), vad=vad)
    else:
        raise ValueError(f"Unsupported STT provider={stt_provider!r}")

    stt = BrainScoreSTT(stt) if _brain_score_enabled() else stt
    stt = LoggingSTT(stt) if LIVEKIT_VERBOSE_LOGGING else stt
    return FinalTranscriptDedupSTT(stt)


def _openbase_cloud_audio_token() -> str:
    try:
        token = MachineTokenManager(WEB_BACKEND_URL).get_machine_token()
    except (AuthLoginRequiredError, AuthTransientError, MachineTokenError) as exc:
        raise OpenbaseCloudAudioAuthenticationError(
            "Openbase Cloud audio is selected, but Openbase Coder could not get "
            "a valid Openbase machine token. Run `openbase-coder login` and "
            "restart the Openbase services, or choose direct provider keys or "
            "local audio in voice settings."
        ) from exc
    if not token:
        raise OpenbaseCloudAudioAuthenticationError(
            "Openbase Cloud audio is selected, but Openbase Coder received an "
            "empty Openbase machine token. Run `openbase-coder login` and restart "
            "the Openbase services, or choose direct provider keys or local audio "
            "in voice settings."
        )
    return token


def _openbase_cloud_audio_http_base_url(provider: str) -> str:
    return f"{OPENBASE_CLOUD_AUDIO_BASE_URL}/{provider}"


def _openbase_cloud_audio_ws_base_url(provider: str) -> str:
    base_url = _openbase_cloud_audio_http_base_url(provider)
    if base_url.startswith("https://"):
        return f"wss://{base_url.removeprefix('https://')}"
    if base_url.startswith("http://"):
        return f"ws://{base_url.removeprefix('http://')}"
    return base_url


def _diagnostic_vad(vad_model):
    if not LIVEKIT_VERBOSE_LOGGING or isinstance(vad_model, LoggingVAD):
        return vad_model
    return LoggingVAD(vad_model)


def _agent_error_code(exc: Exception) -> str:
    if isinstance(exc, LiveVoiceSessionError):
        return LIVE_VOICE_PROVIDER_FAILED_CODE
    if isinstance(exc, OpenbaseCloudAudioSubscriptionError):
        return "subscription_required"
    if isinstance(exc, OpenbaseCloudAudioAuthenticationError | AuthLoginRequiredError):
        return "login_required"
    if isinstance(exc, AuthTransientError):
        return "cloud_unavailable"
    if _is_openbase_cloud_audio_authorization_error(exc):
        return "cloud_audio_auth_failed"
    if _is_openbase_cloud_audio_provider_error(exc):
        return "cloud_audio_provider_failed"
    return "agent_start_failed"


def _agent_error_detail(exc: Exception) -> str:
    if isinstance(exc, LiveVoiceSessionError):
        return (
            "The call ended because the live voice connection was lost: "
            f"{redact_exception_text(exc)}. This is usually a brief service "
            "update, a dropped network, or the live voice login/subscription "
            "being rejected. Call again in a minute; if it keeps happening, "
            "switch the voice model to the classic pipeline in voice settings."
        )
    if isinstance(
        exc,
        OpenbaseCloudAudioSubscriptionError
        | OpenbaseCloudAudioAuthenticationError
        | AuthLoginRequiredError
        | AuthTransientError,
    ):
        return str(exc)
    if _is_openbase_cloud_audio_authorization_error(exc):
        # The audio proxy refuses the websocket handshake with a bare 403 both
        # when the account's allowance is used up and when the token is bad;
        # ``_refine_cloud_audio_error`` asks Openbase Cloud which it was before
        # this text is used, so this wording covers the remaining ambiguity.
        return (
            "Openbase Cloud refused the audio connection for this call. This "
            "usually means this account's monthly Openbase audio allowance is "
            "used up; upgrade your plan at app.openbase.cloud or wait for it to "
            "reset. If credit remains, sign in to Openbase Cloud again on your "
            "computer and restart the Openbase Coder services, or switch voice "
            "settings to direct provider keys or local audio."
        )
    if _is_openbase_cloud_audio_provider_error(exc):
        return (
            "The call ended because Openbase Cloud audio was interrupted — "
            "usually a brief service update. Please call again in a minute. "
            "If your audio credits are used up instead, subscribe or upgrade "
            "at app.openbase.cloud."
        )
    summary = redact_exception_text(exc)
    return (
        "The Openbase voice agent joined the call but could not start its "
        f"audio pipeline: {summary}. Check the voice settings and the Openbase "
        "Coder service logs, then rejoin the call."
    )


async def _refine_cloud_audio_error(exc: Exception) -> Exception:
    """Replace a bare Openbase Cloud audio refusal with the account's real reason.

    The audio proxy closes a refused websocket before the handshake completes,
    so an exhausted allowance and a rejected token both reach the agent as an
    HTTP 403 with no body (an over-cap stream closed mid-call is just as
    mute, and so is a live session the gateway ended for billing). Ask
    Openbase Cloud for the audio usage summary: when it reports the
    credits used up (or a sign-in problem), return that exception so the
    status packet carries the plain reason instead of "authorization failed".
    Any other outcome keeps the original error.
    """
    # A live session that died carries no reason either: the GPT-Live plugin
    # raises a bare "GPT-Live returned an error" for the gateway's fatal
    # billing_hard_limit_reached close, so check the live voice credits too.
    is_live = isinstance(exc, LiveVoiceSessionError)
    if not is_live and not _is_openbase_cloud_audio_provider_error(exc):
        return exc
    try:
        await asyncio.to_thread(
            ensure_openbase_cloud_audio_subscription,
            tts_provider_id=selected_tts_provider_id(),
            stt_provider_id=selected_stt_provider_id(),
            web_backend_url=WEB_BACKEND_URL,
            live_voice=is_live,
        )
    except (OpenbaseCloudAudioSubscriptionError, AuthLoginRequiredError) as reason:
        logger.info(
            "Openbase Cloud audio error refined to %s: %s",
            type(reason).__name__,
            reason,
        )
        reason.__cause__ = exc
        return reason
    except Exception:
        logger.debug(
            "Openbase Cloud audio usage check did not explain the audio error",
            exc_info=True,
        )
    return exc


async def _report_agent_error(room: rtc.Room, exc: Exception) -> None:
    """Best-effort: tell room participants why the agent cannot operate."""
    exc = await _refine_cloud_audio_error(exc)
    try:
        await publish_agent_error_packet(
            room,
            code=_agent_error_code(exc),
            detail=_agent_error_detail(exc),
        )
    except Exception:
        logger.exception("Unable to publish LiveKit agent error packet")


async def _end_call_after_agent_error(ctx: JobContext, exc: Exception) -> None:
    """Boot the call after an unrecoverable error.

    Publish the reason first (the iOS app renders the packet detail verbatim
    and plays its call-ended sound), give the reliable data channel a moment
    to flush, then delete the room so every participant is disconnected
    instead of sitting in a silent, half-dead call."""
    await _report_agent_error(ctx.room, exc)
    await asyncio.sleep(0.75)
    await _delete_room(str(getattr(ctx.room, "name", "") or ""))
    ctx.shutdown(reason="agent-error")


async def _delete_room(room_name: str) -> None:
    """Best-effort room delete so every participant is disconnected.

    On failure the agent still leaves, and LiveKit's empty-room timeout
    eventually ends the call.
    """
    api_key = os.environ.get("LIVEKIT_API_KEY")
    api_secret = os.environ.get("LIVEKIT_API_SECRET")
    if not (room_name and api_key and api_secret):
        return
    import livekit.api as livekit_api

    try:
        client = livekit_api.LiveKitAPI(
            url=os.environ.get("LIVEKIT_URL", "ws://localhost:7880"),
            api_key=api_key,
            api_secret=api_secret,
        )
        try:
            await client.room.delete_room(livekit_api.DeleteRoomRequest(room=room_name))
        finally:
            await client.aclose()
    except Exception:
        logger.exception("Unable to delete LiveKit room %s", room_name)


async def _room_sid(room: rtc.Room) -> str:
    sid = getattr(room, "sid", "") or ""
    if inspect.isawaitable(sid):
        try:
            sid = await sid
        except Exception:
            logger.debug("Unable to resolve LiveKit room sid", exc_info=True)
            return ""
    return str(sid or "")


def _schedule_voice_lifecycle_packet(
    room: rtc.Room, event: str, record, reason: str
) -> None:
    task = asyncio.create_task(
        publish_voice_lifecycle_packet(
            room,
            event=event,
            record=record,
            reason=reason,
        )
    )

    def _log_publish_result(publish_task: asyncio.Task[str]) -> None:
        try:
            publish_task.result()
        except Exception:
            logger.warning(
                "dispatch_timing stage=voice_lifecycle_packet_publish_failed "
                "event=%s delivery_id=%s",
                event,
                getattr(record, "delivery_id", ""),
                exc_info=True,
            )

    task.add_done_callback(_log_publish_result)


def _is_openbase_cloud_audio_authorization_error(exc: Exception) -> bool:
    status = _exception_status(exc)
    return status in {401, 403} and _exception_mentions_openbase_cloud_audio(exc)


def _is_openbase_cloud_audio_provider_error(exc: Exception) -> bool:
    return _exception_mentions_openbase_cloud_audio(exc)


def _exception_status(exc: BaseException) -> int | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status", None) or getattr(
            current, "status_code", None
        )
        if isinstance(status, int):
            return status
        current = current.__cause__ or current.__context__
    return None


def _exception_mentions_openbase_cloud_audio(exc: BaseException) -> bool:
    text = exception_chain_summary(exc).lower()
    return "app.openbase.cloud/api/openbase/audio/" in text or (
        "/api/openbase/audio/" in text and "openbase" in text
    )


# Spoken when the Openbase Cloud audio allowance is used up: short enough to
# finish before anything can interrupt it.
CLOUD_AUDIO_ALLOWANCE_SPOKEN = (
    "This account's monthly Openbase audio allowance is used up."
)


def _uses_openbase_cloud_audio() -> bool:
    return (
        selected_tts_provider_id() == OPENBASE_CLOUD_TTS_PROVIDER_ID
        or selected_stt_provider_id() == OPENBASE_CLOUD_STT_PROVIDER_ID
    )


async def _verify_cloud_audio_subscription(room: rtc.Room, session) -> None:
    """Check the Openbase Cloud audio subscription and surface failures.

    The room token endpoint gates joins on this check, but the subscription
    can lapse (or credits run out) after the token was minted, leaving the
    user in a silent call. Run the check again from the agent and tell the
    participant instead of stalling."""
    try:
        await asyncio.to_thread(
            ensure_openbase_cloud_audio_subscription,
            tts_provider_id=selected_tts_provider_id(),
            stt_provider_id=selected_stt_provider_id(),
            web_backend_url=WEB_BACKEND_URL,
        )
    except (
        OpenbaseCloudAudioSubscriptionError,
        AuthLoginRequiredError,
    ) as exc:
        logger.error("Openbase Cloud audio is unusable for this voice session: %s", exc)
        await _report_agent_error(room, exc)
        try:
            # One short, complete sentence: the live speech gate drops the rest
            # of a reply as soon as the caller is heard, so a long explanation
            # risks ending mid-word. The packet carries the full detail.
            session.say(
                CLOUD_AUDIO_ALLOWANCE_SPOKEN
                if isinstance(exc, OpenbaseCloudAudioSubscriptionError)
                else "Openbase Cloud audio is unavailable for this call. "
                "Check your Openbase sign-in or voice settings."
            )
        except Exception:
            logger.warning(
                "Unable to speak LiveKit agent failure message", exc_info=True
            )
    except AuthTransientError as exc:
        logger.warning(
            "Skipping Openbase Cloud audio subscription check this session: %s", exc
        )


# Delay before re-asserting "thinking" after the session drops to
# "listening" while a backend turn still owes an answer; long enough for an
# in-flight cancellation to settle, well under the ~0.9s the iOS app waits
# before auto-unmuting.
ANSWER_OWED_STATE_RECHECK_SECONDS = 0.25
ANSWER_OWED_STATE_MONITOR_INTERVAL_SECONDS = 1.0


def _register_answer_owed_state_hold(
    session: AgentSession, voice_router: LiveKitVoiceRouter
) -> None:
    """Keep the agent state at "thinking" while an answer is still owed.

    When the voice-side generation dies (interruption or poll failure) the
    session drops to "listening" and the iOS app auto-unmutes as if the
    assistant were done, even though the backend turn is still going to
    produce an answer. Hold "thinking" until the answer is delivered or the
    owed turn goes away, then hand the state machine back to the framework.
    """
    hold = {"active": False}

    def _active_client_has_pending_answer() -> bool:
        if voice_router.delivery_ledger is not None:
            ledger_pending = (
                voice_router.delivery_ledger.has_pending_delivery_for_current_route()
            )
            legacy_pending = False
            has_pending = getattr(
                voice_router.active_client, "has_pending_voice_answer", None
            )
            if callable(has_pending):
                legacy_pending = bool(has_pending())
            logger.info(
                "dispatch_timing stage=answer_owed_hold_pending_check "
                "source=ledger ledger_pending=%s legacy_pending=%s",
                ledger_pending,
                legacy_pending,
            )
            return ledger_pending
        has_pending = getattr(
            voice_router.active_client, "has_pending_voice_answer", None
        )
        legacy_pending = callable(has_pending) and bool(has_pending())
        logger.info(
            "dispatch_timing stage=answer_owed_hold_pending_check "
            "source=legacy ledger_pending=false legacy_pending=%s",
            legacy_pending,
        )
        return legacy_pending

    async def _monitor_hold() -> None:
        while hold["active"]:
            await asyncio.sleep(ANSWER_OWED_STATE_MONITOR_INTERVAL_SECONDS)
            if not hold["active"]:
                return
            if _active_client_has_pending_answer():
                continue
            hold["active"] = False
            if session.agent_state == "thinking" and session.current_speech is None:
                logger.info(
                    "dispatch_timing stage=agent_state_hold_released "
                    "reason=no_pending_answer"
                )
                session._update_agent_state("listening")

    async def _reassert_thinking() -> None:
        await asyncio.sleep(ANSWER_OWED_STATE_RECHECK_SECONDS)
        if hold["active"] or not _active_client_has_pending_answer():
            return
        if session.agent_state != "listening" or session.user_state == "speaking":
            return
        logger.info(
            "dispatch_timing stage=agent_state_held_thinking reason=answer_owed"
        )
        hold["active"] = True
        session._update_agent_state("thinking")
        asyncio.create_task(_monitor_hold())

    def _on_agent_state_changed(event) -> None:
        if getattr(event, "new_state", None) == "listening":
            asyncio.create_task(_reassert_thinking())

    def _on_speech_created(_event) -> None:
        # A real generation or direct say() is driving the state machine
        # again; stop holding.
        hold["active"] = False

    session.on("agent_state_changed", _on_agent_state_changed)
    session.on("speech_created", _on_speech_created)


def _register_orphaned_result_delivery(
    session: AgentSession, voice_router: LiveKitVoiceRouter
) -> None:
    """Speak completed turn answers that no voice dispatch delivered."""

    def _deliver(client, turn_id: str, speech_text: str) -> None:
        delivery_ledger = voice_router.delivery_ledger
        if delivery_ledger is not None:
            record = delivery_ledger.record_for_turn(turn_id)
            if record is not None:
                if not record.tts_text_hash:
                    from openbase_coder_cli.livekit_agent.tts_selection import (
                        text_for_tts,
                    )

                    delivery_ledger.mark_text_generated(
                        record,
                        speech_text=speech_text,
                        tts_text=text_for_tts(speech_text),
                    )
                if not delivery_ledger.reserve_for_tts(record):
                    logger.info(
                        "dispatch_timing stage=orphaned_result_skipped turn_id=%s "
                        "reason=delivery_ledger_rejected",
                        turn_id,
                    )
                    return
            elif not voice_router.claim_speech(client, turn_id):
                logger.info(
                    "dispatch_timing stage=orphaned_result_skipped turn_id=%s "
                    "reason=inactive_client_or_already_spoken",
                    turn_id,
                )
                return
        elif not voice_router.claim_speech(client, turn_id):
            logger.info(
                "dispatch_timing stage=orphaned_result_skipped turn_id=%s "
                "reason=inactive_client_or_already_spoken",
                turn_id,
            )
            return
        logger.info(
            "dispatch_timing stage=orphaned_result_spoken turn_id=%s speech_chars=%d",
            turn_id,
            len(speech_text),
        )
        try:
            session.say(speech_text)
        except Exception:
            client.release_speech_claim(turn_id)
            logger.warning("Unable to speak orphaned voice result", exc_info=True)

    voice_router.set_orphaned_result_handler(_deliver)


async def _start_voice_session(
    ctx: JobContext,
    voice_router: LiveKitVoiceRouter,
    delivery_ledger: VoiceDeliveryLedger,
) -> tuple[AgentSession, "VoiceSelectingTTS", tuple]:
    """Build the STT/TTS pipeline and start the agent session in the room."""
    from openbase_coder_cli.livekit_agent.live_call_lifecycle import (
        bind_live_call_lifecycle,
    )

    dispatcher_voice = dispatcher_voice_config()
    tts_provider = get_tts_provider(dispatcher_voice.provider)
    credentials = _tts_credentials(tts_provider)
    direct_tts = VoiceSelectingTTS(
        default_voice_id=dispatcher_voice.voice_id,
        default_voice_name=dispatcher_voice.name,
        active_voice_id=lambda: voice_router.active_target_voice_id,
        active_voice_name=lambda: voice_router.active_target_voice_name,
        provider=tts_provider,
        role="direct",
        delivery_ledger=delivery_ledger,
        **credentials,
    )
    announcer_tts = _build_announcer_tts(voice_router, tts_provider, credentials)

    session_vad = _diagnostic_vad(ctx.proc.userdata["vad"])
    turn_signal_tracker = VoiceTurnSignalTracker()

    # Set up a voice AI pipeline
    session = AgentSession(
        conn_options=voice_connect_options(),
        stt=_build_stt(session_vad),
        llm=CodexLiveKitLLM(
            voice_router,
            turn_signal_tracker=turn_signal_tracker,
        ),
        tts=direct_tts,
        turn_handling={
            "turn_detection": SafeMultilingualModel(
                turn_signal_tracker=turn_signal_tracker,
            ),
            "interruption": {"mode": "vad"},
            # livekit-agents 1.8 deprecated the top-level kwarg; same semantics.
            "preemptive_generation": {"enabled": False},
        },
        vad=session_vad,
    )
    bind_live_call_lifecycle(ctx, session, delete_room=_delete_room)
    session_diagnostic_handlers = _register_session_diagnostics(
        session,
        voice_router,
        enable_logging=LIVEKIT_VERBOSE_LOGGING,
        on_unrecoverable_error=lambda exc: _end_call_after_agent_error(ctx, exc),
    )

    # Start the session
    await session.start(
        agent=Assistant(),
        room=ctx.room,
    )
    logger.info(
        "dispatch_timing stage=agent_session_start_complete room_name=%s "
        "stt_provider=%s tts_role=direct",
        ctx.room.name,
        selected_stt_provider_id(),
    )
    return session, announcer_tts, session_diagnostic_handlers


class LiveVoiceAssistant(SpeechGatedAgent):
    """The LiveKit agent for the live engine: GPT-Live persona plus the bridge.

    The persona is fixed at ``session.start``; everything that changes during
    the call (route, agent names, results) reaches the model through the
    delegation bridge's appends. ``on_enter`` runs once the duplex session
    exists, which is the earliest point the bridge can subscribe to the
    plugin's closed caller utterances (every one goes to the active agent)
    and ``delegation_created``.
    """

    def __init__(self, bridge: LiveDelegationBridge) -> None:
        super().__init__(
            instructions=live_voice_startup_instructions(
                agent_label=bridge.starting_agent_label()
            )
        )
        self._bridge = bridge
        self._speech_gate = bridge.speech_gate

    async def on_enter(self) -> None:
        self._bridge.attach(self.duplex_session)


async def _decide_voice_engine_for_call() -> VoiceEngineDecision:
    """Read the voice model per call and check the live engine can start.

    A fresh readiness-cache entry (see ``prewarm``) stands in for the
    entitlement check and the handshake probe.
    """
    return await decide_voice_engine(
        selected_engine=selected_voice_engine(),
        tts_provider_id=selected_tts_provider_id(),
        stt_provider_id=selected_stt_provider_id(),
        cloud_token_provider=_openbase_cloud_audio_token,
        readiness_cache=_live_voice_readiness_cache,
    )


def _build_live_voice_model(
    decision: VoiceEngineDecision, *, preconnect: bool = False, voice: str | None = None
):
    gpt_live_model = import_live_model()
    if preconnect:
        gpt_live_model = _preconnecting_model_class(gpt_live_model)
    credentials = decision.credentials
    assert credentials is not None
    from openbase_coder_cli.voice_identity import current_voice_identity

    selected_voice = voice or current_voice_identity().gpt_live_voice
    model = gpt_live_model(
        model=LIVE_VOICE_MODEL,
        voice=selected_voice,
        delegation="client",
        api_key=credentials.api_key,
        base_url=credentials.base_url,
    )
    model._openbase_voice = selected_voice
    return model


async def _prepare_live_voice_model(
    decision_task: "asyncio.Task[VoiceEngineDecision]",
) -> Any | None:
    """Build the live model and open its gateway connection as early as possible.

    Resolves to the model once the engine decision is live, or None (pipeline,
    preconnect disabled, or the preconnect itself failing: the session start
    then connects as before).
    """
    decision = await decision_task
    if not decision.is_live or not LIVE_VOICE_PRECONNECT:
        return None
    live_model = _build_live_voice_model(decision, preconnect=True)
    try:
        session = live_model.preconnect()
    except Exception:
        logger.warning(
            "dispatch_timing stage=live_session_preconnect_failed", exc_info=True
        )
        return live_model
    if session is not None:
        logger.info("dispatch_timing stage=live_session_preconnect_start")
    return live_model


async def _discard_live_voice_model(live_model_task: "asyncio.Task[Any]") -> None:
    if not live_model_task.done():
        live_model_task.cancel()
    try:
        live_model = await live_model_task
    except (asyncio.CancelledError, Exception):
        return
    discard = getattr(live_model, "discard_preconnected", None)
    if discard is not None:
        await discard()
    if live_model is not None:
        await live_model.aclose()


async def _start_live_voice_session(
    ctx: JobContext,
    voice_router: LiveKitVoiceRouter,
    delivery_ledger: VoiceDeliveryLedger,
    decision: VoiceEngineDecision,
    *,
    live_model: Any | None = None,
) -> tuple[AgentSession, LiveDelegationBridge, tuple]:
    """Start the GPT-Live full-duplex session with the delegation bridge.

    ``live_model`` is the model prepared by ``_prepare_live_voice_model``
    (its gateway connection already opening); None builds one here.
    """
    from openbase_coder_cli.livekit_agent.live_call_lifecycle import (
        bind_live_call_lifecycle,
    )
    from openbase_coder_cli.livekit_agent.live_characters import (
        LiveCharacterController,
        log_character_started,
    )
    from openbase_coder_cli.voice_identity import route_voice_identity

    identity = route_voice_identity(voice_router)
    if (
        live_model is not None
        and getattr(live_model, "_openbase_voice", identity.gpt_live_voice)
        != identity.gpt_live_voice
    ):
        await live_model.discard_preconnected()
        await live_model.aclose()
        live_model = None
    if live_model is None:
        live_model = _build_live_voice_model(decision, voice=identity.gpt_live_voice)
    session_start = time.monotonic()
    session_vad = _diagnostic_vad(ctx.proc.userdata["vad"])
    # The model listens continuously. VAD drives our explicit output gate:
    # the duplex adapter permits overlap and cannot cancel provider output.
    session = AgentSession(
        conn_options=voice_connect_options(),
        llm=live_model,
        vad=session_vad,
        turn_handling={"interruption": {"mode": "vad"}},
    )
    bind_live_call_lifecycle(ctx, session, delete_room=_delete_room)
    bridge = LiveDelegationBridge(
        voice_router=voice_router,
        delivery_ledger=delivery_ledger,
        call_id=str(getattr(ctx.room, "name", "") or ""),
        initial_agent_label=_route_agent_label(voice_router),
    )
    live_ready = False

    async def handle_live_error(exc: Exception) -> None:
        if live_ready:
            await _end_call_after_agent_error(ctx, LiveVoiceSessionError(exc))

    session_diagnostic_handlers = _register_session_diagnostics(
        session,
        voice_router,
        enable_logging=LIVEKIT_VERBOSE_LOGGING,
        on_unrecoverable_error=handle_live_error,
        proactive_steering=False,
    )

    def gate_caller_speech(event):
        bridge.speech_gate.user_state_changed(event.new_state)
        if event.new_state == "speaking":
            # Settle the barge-in once the caller has spoken long enough; a
            # shorter blip is echo or noise and the answer plays through it.
            # Over the agent's own audio the gate waits for a transcript.
            asyncio.get_running_loop().call_later(
                bridge.speech_gate.barge_in_min_seconds, _check_barge_in
            )

    def _check_barge_in():
        bridge.speech_gate.speaking  # noqa: B018 - evaluating revokes

    # The announcer's words are what its echo transcribes to, like the
    # model's; every session.say on this session goes through here.
    session_say = getattr(session, "say", None)

    def say_and_note_words(text, *args, **kwargs):
        gate = bridge.speech_gate
        if isinstance(text, str):
            gate.agent_said(text)
        gate.playout_requested()
        try:
            handle = session_say(text, *args, **kwargs)
        except BaseException:
            gate.playout_finished()
            raise
        add_done = getattr(handle, "add_done_callback", None)
        if callable(add_done):
            add_done(lambda _handle: gate.playout_finished())
        else:
            gate.playout_finished()
        return handle

    if session_say is not None:
        session.say = say_and_note_words

    # Clear queued playout once the caller is really interrupting. The gate
    # may decide inside the audio node it is filtering, so interrupt from the
    # loop rather than from within that generator.
    bridge.speech_gate.on_barge_in = lambda: asyncio.get_running_loop().call_soon(
        lambda: session.interrupt(force=True)
    )

    # Bind before start, including callers who speak over the first greeting.
    session.on("user_state_changed", gate_caller_speech)
    session_diagnostic_handlers += (("user_state_changed", gate_caller_speech),)
    logger.info(
        "dispatch_timing stage=agent_session_start_begin room_name=%s "
        "voice_engine=live readiness=%s",
        ctx.room.name,
        decision.readiness,
    )
    try:
        assistant = LiveVoiceAssistant(bridge)
        await session.start(agent=assistant, room=ctx.room)
        await wait_live_session_started(
            assistant.duplex_session, timeout=LIVE_VOICE_PREFLIGHT_TIMEOUT_SECONDS
        )
        log_character_started(identity, assistant.duplex_session, voice_router)
        bridge.greet(live_voice_greeting(bridge.starting_agent_label()))
        bridge.brief_active_thread()
        live_ready = True
        characters = LiveCharacterController(
            session=session,
            bridge=bridge,
            router=voice_router,
            model_factory=lambda voice: _build_live_voice_model(
                decision, voice=voice, preconnect=LIVE_VOICE_PRECONNECT
            ),
            instructions=lambda label: live_voice_startup_instructions(
                agent_label=label
            ),
            on_error=handle_live_error,
            ledger=delivery_ledger,
            initial_model=live_model,
            speech_gate=bridge.speech_gate,
            timeout=LIVE_VOICE_CHARACTER_START_TIMEOUT_SECONDS,
            start_attempts=LIVE_VOICE_CHARACTER_START_ATTEMPTS,
            announce_route=getattr(
                _live_route_announcer(voice_router), "announce", None
            ),
        )
        bridge.characters = characters
        bridge.character_route_changed = characters.route_changed
        characters.start()
    except BaseException:
        await bridge.aclose()
        for event_name, handler in session_diagnostic_handlers:
            session.off(event_name, handler)
        try:
            await session.aclose()
        except Exception:
            logger.debug("live AgentSession close after failed start", exc_info=True)
        discard = getattr(live_model, "discard_preconnected", None)
        if discard is not None:
            await discard()
        await live_model.aclose()
        raise
    logger.info(
        "dispatch_timing stage=agent_session_start_complete room_name=%s "
        "voice_engine=live live_base_url=%s readiness=%s elapsed_ms=%d",
        ctx.room.name,
        decision.credentials.base_url if decision.credentials else "",
        decision.readiness,
        int((time.monotonic() - session_start) * 1000),
    )
    return session, bridge, session_diagnostic_handlers


def _build_delivery_ledger(
    ctx: JobContext,
    voice_router: LiveKitVoiceRouter,
    *,
    room_id: str,
    live_mode: bool,
) -> VoiceDeliveryLedger:
    delivery_ledger = VoiceDeliveryLedger(
        route_snapshot=voice_router.route_snapshot,
        room_name=ctx.room.name,
        room_id=room_id,
        live_mode=live_mode,
    )
    delivery_ledger.set_lifecycle_sink(
        lambda event, record, reason: _schedule_voice_lifecycle_packet(
            ctx.room,
            event,
            record,
            reason,
        )
    )
    voice_router.delivery_ledger = delivery_ledger
    return delivery_ledger


async def _report_live_voice_fallback(
    room: rtc.Room, decision: VoiceEngineDecision
) -> None:
    """Non-fatal notice: the call continues on the pipeline engine."""
    try:
        await publish_agent_error_packet(
            room,
            code=LIVE_VOICE_UNAVAILABLE_CODE,
            detail=live_voice_unavailable_detail(decision),
            severity="warning",
        )
    except Exception:
        logger.exception("Unable to publish the live voice fallback packet")


async def _publish_voice_engine(room: rtc.Room, engine: str) -> None:
    try:
        await publish_voice_engine_attribute(room, engine)
    except Exception:
        logger.warning(
            "Unable to publish the %s participant attribute",
            VOICE_ENGINE_ATTRIBUTE,
            exc_info=True,
        )


async def _transfer_live_voice_route(
    voice_router: LiveKitVoiceRouter,
    route_command: VoiceRouteCommand,
    bridge: LiveDelegationBridge,
) -> None:
    assert route_command.thread_id is not None
    assert route_command.cwd is not None
    try:
        transferred = await voice_router.transfer_to_thread(
            thread_id=route_command.thread_id,
            cwd=route_command.cwd,
            label=route_command.label,
            voice_id=route_command.active_target_voice_id,
            voice_name=route_command.active_target_voice_name,
        )
    except Exception:
        logger.warning("Unable to transfer LiveKit voice route", exc_info=True)
        voice_router.exit_to_dispatch()
        bridge.notify_route_changed(action="exit_to_dispatch", agent_label=None)
        bridge.announce("Unable to transfer voice route.")
        return
    if transferred is False:
        return
    bridge.notify_route_changed(
        action="transfer_to_thread",
        agent_label=route_command.active_target_voice_name or route_command.label,
        announce=route_command.announce,
    )


def _wire_live_voice_call(
    ctx: JobContext,
    session: AgentSession,
    bridge: LiveDelegationBridge,
    voice_router: LiveKitVoiceRouter,
    delivery_ledger: VoiceDeliveryLedger,
    session_diagnostic_handlers: tuple,
    room_diagnostic_handlers: tuple,
) -> None:
    """Room and session plumbing for the live engine.

    The mic stays open. Character sessions own mapped announcements and
    transfer handoffs; audio-file announcements use the existing queue.
    Caller utterances (prompts and spoken commands) reach
    the bridge straight from the plugin session it attached to, not from
    ``user_input_transcribed`` here, so each one is handled exactly once.
    """

    def on_agent_state_changed(event) -> None:
        # Any agent audio, announcer clips included, echoes on a speakerphone.
        bridge.speech_gate.agent_state_changed(
            str(getattr(event, "new_state", "") or "")
        )
        if hasattr(bridge, "characters"):
            bridge.characters.state_changed(event)
            if bridge.characters.announcing:
                return
        bridge.on_agent_state_changed(
            str(getattr(event, "old_state", "") or ""),
            str(getattr(event, "new_state", "") or ""),
        )

    session.on("agent_state_changed", on_agent_state_changed)

    def on_user_state_changed(event) -> None:
        if hasattr(bridge, "characters"):
            bridge.characters.user_state_changed(event)
        bridge.on_user_state_changed(
            str(getattr(event, "old_state", "") or ""),
            str(getattr(event, "new_state", "") or ""),
        )

    session.on("user_state_changed", on_user_state_changed)

    audio_queue = AnnouncerSpeechQueue(
        session=session,
        announcer_tts=None,
        delivery_ledger=delivery_ledger,
    )
    audio_queue_session_handlers = (
        ("user_state_changed", audio_queue.notify_state_changed),
        ("agent_state_changed", audio_queue.notify_state_changed),
        ("speech_created", audio_queue.notify_state_changed),
    )
    for event_name, handler in audio_queue_session_handlers:
        session.on(event_name, handler)
    audio_queue.start()
    voice_router.set_orphaned_result_handler(bridge.deliver_orphaned_result)

    def on_data_received(data_packet: rtc.DataPacket) -> None:
        logger.info(
            "dispatch_timing stage=livekit_data_received topic=%s kind=%s "
            "payload_bytes=%d payload_hash=%s participant_identity=%s engine=live",
            data_packet.topic,
            data_packet.kind,
            len(data_packet.data),
            _packet_hash(data_packet),
            _packet_participant_identity(data_packet),
        )
        message = parse_announcer_packet(data_packet)
        if message is not None:
            logger.info(
                "dispatch_timing stage=announcer_packet_received message_id=%s "
                "agent_name=%s text_len=%d payload_hash=%s engine=live",
                message.message_id,
                message.agent_name or "",
                len(message.text),
                _packet_hash(data_packet),
            )
            bridge.characters.announce(message)
            return
        audio_message = parse_announcer_audio_packet(data_packet)
        if audio_message is not None:
            audio_queue.enqueue(audio_message)
            return
        route_command = parse_voice_route_packet(data_packet)
        if route_command is None:
            return
        logger.info(
            "dispatch_timing stage=voice_route_packet_received action=%s "
            "thread_id=%s label=%s engine=live payload_hash=%s",
            route_command.action,
            route_command.thread_id or "",
            route_command.label or "",
            _packet_hash(data_packet),
        )
        if route_command.action == "exit_to_dispatch":
            if voice_router.exit_to_dispatch():
                bridge.notify_route_changed(action="exit_to_dispatch", agent_label=None)
        elif route_command.action == "transfer_to_thread":
            if not route_command.thread_id or not route_command.cwd:
                logger.warning(
                    "Ignoring incomplete LiveKit voice route transfer command"
                )
                return
            asyncio.create_task(
                _transfer_live_voice_route(voice_router, route_command, bridge)
            )
        else:
            logger.warning(
                "Ignoring unsupported LiveKit voice route action %s",
                route_command.action,
            )

    ctx.room.on("data_received", on_data_received)

    async def close_live_call(*_args) -> None:
        ctx.room.off("data_received", on_data_received)
        for event_name, handler in room_diagnostic_handlers:
            ctx.room.off(event_name, handler)
        for event_name, handler in session_diagnostic_handlers:
            session.off(event_name, handler)
        for event_name, handler in audio_queue_session_handlers:
            session.off(event_name, handler)
        session.off("agent_state_changed", on_agent_state_changed)
        session.off("user_state_changed", on_user_state_changed)
        if hasattr(bridge, "characters"):
            await bridge.characters.close()
        await bridge.aclose()
        await audio_queue.close()
        await voice_router.close()

    ctx.add_shutdown_callback(close_live_call)


@server.rtc_session(agent_name=LIVEKIT_DISPATCH_AGENT_NAME)
async def livekit_agent(ctx: JobContext):
    from openbase_coder_cli.services.livekit_pool_activity import record_activity

    job_received = time.monotonic()
    record_activity("job")
    _live_voice_readiness_refresher.stop()
    _refresh_audio_credentials()
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }
    _log_job_received(ctx)
    logger.info(
        "Connecting LiveKit voice session to Super Agents backend with cwd=%s",
        LIVEKIT_CODEX_THREAD_CWD,
    )
    voice_backend_client = (
        _build_voice_backend_client(persist_thread=False)
        if LIVEKIT_CODEX_FRESH_THREAD_PER_SESSION
        else _shared_voice_backend_client
    )
    prepare_task = asyncio.create_task(voice_backend_client.prepare())
    prepare_task.add_done_callback(_log_prepare_result)
    # Warm the dispatcher's backend session (Claude CLI resume, auth check)
    # while the call starts, so the first turn does not pay it (F6: 2.4 s).
    warm_task = asyncio.create_task(
        _warm_voice_backend(voice_backend_client, prepare_task, room_name=ctx.room.name)
    )

    async def _cancel_warm_task() -> None:
        warm_task.cancel()

    ctx.add_shutdown_callback(_cancel_warm_task)
    voice_router = LiveKitVoiceRouter(voice_backend_client)
    # Read the voice model per call (no restart needed) and check the live
    # engine's prerequisites while the room connects; a live engine that
    # cannot start falls back to the pipeline for this call.
    decision_task = asyncio.create_task(_decide_voice_engine_for_call())
    # Open the GPT-Live gateway connection as soon as the engine is decided,
    # while the room connects, instead of inside AgentSession.start.
    live_model_task = asyncio.create_task(_prepare_live_voice_model(decision_task))

    logger.info("Connecting to LiveKit room")
    try:
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    except Exception:
        decision_task.cancel()
        warm_task.cancel()
        await _discard_live_voice_model(live_model_task)
        logger.error(
            "LiveKit agent failed to connect to room %s; participants will "
            "stay at 'waiting for agent'",
            ctx.room.name,
            exc_info=True,
        )
        raise
    # Keep the exact "Connected to LiveKit room" wording: the troubleshooting
    # runbook greps for it.
    logger.info(
        "Connected to LiveKit room "
        "dispatch_timing stage=agent_room_connected room_name=%s since_job_ms=%d",
        ctx.room.name,
        int((time.monotonic() - job_received) * 1000),
    )
    _watch_participant_join(ctx, job_received=job_received)
    focused_thread_tracker = FocusedThreadTracker()
    focused_thread_tracker.attach(ctx.room)
    voice_router.focused_thread_tracker = focused_thread_tracker

    async def _detach_focused_thread_tracker() -> None:
        focused_thread_tracker.detach()

    ctx.add_shutdown_callback(_detach_focused_thread_tracker)
    room_diagnostic_handlers = (
        _register_room_diagnostics(ctx.room) if LIVEKIT_VERBOSE_LOGGING else ()
    )
    start_route_failure = await _apply_start_route(
        ctx, voice_router, prepare_task=prepare_task
    )
    decision = await decision_task
    logger.info(
        "dispatch_timing stage=voice_engine_decided room_name=%s engine=%s "
        "readiness=%s fallback_reason=%s since_job_ms=%d",
        ctx.room.name,
        decision.engine,
        decision.readiness,
        decision.fallback_reason or "",
        int((time.monotonic() - job_received) * 1000),
    )
    room_id = await _room_sid(ctx.room)
    delivery_ledger = _build_delivery_ledger(
        ctx, voice_router, room_id=room_id, live_mode=decision.is_live
    )

    live_bridge: LiveDelegationBridge | None = None
    if decision.is_live:
        try:
            (
                session,
                live_bridge,
                session_diagnostic_handlers,
            ) = await _start_live_voice_session(
                ctx,
                voice_router,
                delivery_ledger,
                decision,
                live_model=await live_model_task,
            )
        except Exception as exc:
            await _discard_live_voice_model(live_model_task)
            summary = exception_chain_summary(exc)
            # The cached readiness was wrong about the gateway: the next
            # call probes again instead of trusting it.
            _live_voice_readiness_cache.clear()
            logger.warning(
                "Live voice session failed to start in room %s (%s); falling back "
                "to the pipeline voice engine for this call",
                ctx.room.name,
                summary,
            )
            decision = VoiceEngineDecision(
                engine=VOICE_ENGINE_PIPELINE,
                fallback_reason="live_session_start_failed",
                fallback_detail=f"The live voice session could not start: {summary}.",
            )
            delivery_ledger = _build_delivery_ledger(
                ctx, voice_router, room_id=room_id, live_mode=False
            )
    if live_bridge is not None:
        await _publish_voice_engine(ctx.room, VOICE_ENGINE_LIVE)
        _wire_live_voice_call(
            ctx,
            session,
            live_bridge,
            voice_router,
            delivery_ledger,
            session_diagnostic_handlers,
            room_diagnostic_handlers,
        )
    else:
        try:
            (
                session,
                announcer_tts,
                session_diagnostic_handlers,
            ) = await _start_voice_session(ctx, voice_router, delivery_ledger)
        except Exception as exc:
            logger.error(
                "LiveKit agent joined room %s but could not start its voice session: %s",
                ctx.room.name,
                exception_chain_summary(exc),
            )
            await _end_call_after_agent_error(ctx, exc)
            raise
        if decision.fell_back:
            await _report_live_voice_fallback(ctx.room, decision)
        await _publish_voice_engine(ctx.room, VOICE_ENGINE_PIPELINE)
        _wire_pipeline_voice_call(
            ctx,
            session,
            announcer_tts,
            voice_router,
            delivery_ledger,
            session_diagnostic_handlers,
            room_diagnostic_handlers,
        )

    if start_route_failure:
        _announce_start_route_failure(session, live_bridge, start_route_failure)

    # Surface stalled agent turns during the call — e.g. a spawned sub-agent
    # blocked on a macOS permission dialog on the user's computer (field-test
    # finding FT-9, 2026-09-12). Runs session-wide so it covers the dispatcher
    # and any fire-and-forgotten sub-agent alike.
    from . import stall_diagnostics

    if live_bridge is not None:
        # GPT-Live: the hint is commentary for the current character, which
        # keeps the call and its voice; the classic announcer path would swap
        # in an announcement session (and has no voice for "Dispatcher").
        stall_hint_kwargs = {"speak": _live_stall_hint_speaker(live_bridge)}
    else:
        stall_hint_kwargs = {
            "prepare_announcement": lambda: (
                stall_diagnostics.interrupt_nonplaying_reply(session)
            )
        }
    stall_task = asyncio.create_task(
        stall_diagnostics.stall_watch_loop(
            is_call_active=lambda: (
                ctx.room.connection_state == rtc.ConnectionState.CONN_CONNECTED
            ),
            **stall_hint_kwargs,
        ),
        name="openbase-stall-watch-loop",
    )

    async def _cancel_stall_watch():
        stall_task.cancel()

    ctx.add_shutdown_callback(_cancel_stall_watch)
    logger.info(
        "LiveKit AgentSession started (voice_engine=%s) "
        "dispatch_timing stage=agent_call_ready room_name=%s engine=%s "
        "since_job_ms=%d",
        decision.engine,
        ctx.room.name,
        decision.engine,
        int((time.monotonic() - job_received) * 1000),
    )


def _tts_credentials(tts_provider) -> dict:
    """Cartesia access for the configured provider: Openbase Cloud's audio
    proxy with a short-lived token (plus a refresher, so websocket reconnects
    later in the session stay authenticated), or a local key."""
    openbase_cloud_audio_token = (
        _openbase_cloud_audio_token()
        if tts_provider.provider_id == OPENBASE_CLOUD_TTS_PROVIDER_ID
        else ""
    )
    return {
        "api_key": openbase_cloud_audio_token or CARTESIA_API_KEY,
        "api_key_provider": _openbase_cloud_audio_token
        if openbase_cloud_audio_token
        else None,
        "base_url": _openbase_cloud_audio_http_base_url("cartesia")
        if openbase_cloud_audio_token
        else None,
        "api_version": OPENBASE_CLOUD_AUDIO_CARTESIA_VERSION
        if openbase_cloud_audio_token
        else None,
    }


def _build_announcer_tts(
    voice_router, tts_provider, credentials: dict
) -> VoiceSelectingTTS:
    """The announcer voice both engines speak route moves and notices with."""
    announcer_voice = (
        tts_provider.voice_for_id(CARTESIA_ANNOUNCER_VOICE_ID)
        if tts_provider.provider_id == CARTESIA_PROVIDER_ID
        else None
    ) or tts_provider.default_announcer_voice()
    return VoiceSelectingTTS(
        default_voice_id=announcer_voice.id,
        default_voice_name=announcer_voice.name,
        active_voice_id=lambda: voice_router.active_target_voice_id,
        active_voice_name=lambda: voice_router.active_target_voice_name,
        provider=tts_provider,
        role="announcer",
        **credentials,
    )


def _live_route_announcer(voice_router) -> "RouteAnnouncer | None":
    """Classic's announcer TTS for a live call's transfer and return words."""
    from openbase_coder_cli.livekit_agent.route_announcements import RouteAnnouncer

    try:
        tts_provider = get_tts_provider(dispatcher_voice_config().provider)
        tts = _build_announcer_tts(
            voice_router, tts_provider, _tts_credentials(tts_provider)
        )
    except Exception:
        # The call goes on without spoken route moves (no TTS provider or
        # credentials configured for this install).
        logger.warning(
            "dispatch_timing stage=live_route_announcer_unavailable", exc_info=True
        )
        return None
    return RouteAnnouncer(tts=tts)


def _dispatcher_voice_id() -> str | None:
    """The Dispatcher's assigned voice, for announcements spoken as the Dispatcher."""
    from openbase_coder_cli.livekit_voice_route import get_livekit_voice_route_state

    try:
        return get_livekit_voice_route_state().dispatcher_voice_id or None
    except Exception:  # noqa: BLE001 - a missing voice falls back to the announcer default
        logger.debug("dispatcher voice unavailable for announcement", exc_info=True)
        return None


def _live_stall_hint_speaker(live_bridge: LiveDelegationBridge):
    """Speak a blocked-turn hint through the live character, keeping the call."""

    async def speak(text: str) -> bool:
        live_bridge.announce(text)
        logger.info(
            "dispatch_timing stage=live_stall_hint_announced text_len=%d", len(text)
        )
        return True

    return speak


def _log_job_received(ctx: JobContext) -> None:
    """Stamp the job's arrival; ``room_age_ms`` is the dispatch latency.

    The room's creation time (set when the token view dispatched the agent,
    or when the phone created the room on join) to this job starting is the
    part of the start-up that happens before any of our code runs.
    """
    job = getattr(ctx, "job", None)
    room_info = getattr(job, "room", None)
    created_ms = int(getattr(room_info, "creation_time_ms", 0) or 0)
    if not created_ms:
        created_ms = int(getattr(room_info, "creation_time", 0) or 0) * 1000
    room_age_ms = int(time.time() * 1000) - created_ms if created_ms else -1
    logger.info(
        "dispatch_timing stage=agent_job_received room_name=%s job_id=%s "
        "dispatch_id=%s room_age_ms=%d",
        ctx.room.name,
        getattr(job, "id", "") or "",
        getattr(job, "dispatch_id", "") or "",
        room_age_ms,
    )


async def _warm_voice_backend(
    voice_backend_client: SuperAgentsLiveKitClient,
    prepare_task: "asyncio.Task[str]",
    *,
    room_name: str,
) -> None:
    """Fire-and-forget warm-up of the dispatcher's backend session.

    Waits for the thread to be prepared (the sibling start-route logic also
    awaits that task), then asks the client to connect its backend session so
    the first ``run_turn`` finds it open. Never raises: a failed warm-up just
    means the first turn connects as before.
    """
    if not LIVEKIT_DISPATCHER_WARMUP:
        return
    started = time.monotonic()
    try:
        await prepare_task
    except Exception:
        return
    warm = getattr(voice_backend_client, "warm", None)
    if warm is None:
        return
    try:
        warmed = await warm()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "dispatch_timing stage=dispatcher_session_warm_failed room_name=%s "
            "elapsed_ms=%d",
            room_name,
            int((time.monotonic() - started) * 1000),
            exc_info=True,
        )
        return
    logger.info(
        "dispatch_timing stage=dispatcher_session_warm_complete room_name=%s "
        "warmed=%s elapsed_ms=%d",
        room_name,
        warmed,
        int((time.monotonic() - started) * 1000),
    )


def _watch_participant_join(ctx: JobContext, *, job_received: float) -> None:
    """Log the caller's arrival and end a call nobody joins.

    With explicit dispatch at token time the agent is usually in the room
    before the caller. A token that is never used (the app died, the network
    dropped) would otherwise leave a live session running until the room's
    empty timeout; after ``LIVEKIT_PARTICIPANT_JOIN_TIMEOUT_SECONDS`` without
    a caller the job shuts down and deletes the room.
    """
    room = ctx.room
    joined = asyncio.Event()

    def on_participant_connected(participant) -> None:
        if participant.kind not in DEFAULT_PARTICIPANT_KINDS:
            return
        if joined.is_set():
            return
        joined.set()
        logger.info(
            "dispatch_timing stage=participant_joined room_name=%s identity=%s "
            "since_job_ms=%d",
            room.name,
            getattr(participant, "identity", "") or "",
            int((time.monotonic() - job_received) * 1000),
        )

    room.on("participant_connected", on_participant_connected)
    remote = getattr(room, "remote_participants", None) or {}
    for participant in list(remote.values()):
        on_participant_connected(participant)

    async def watch() -> None:
        try:
            await asyncio.wait_for(
                joined.wait(), LIVEKIT_PARTICIPANT_JOIN_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning(
                "dispatch_timing stage=participant_join_timeout room_name=%s "
                "timeout_s=%s",
                room.name,
                LIVEKIT_PARTICIPANT_JOIN_TIMEOUT_SECONDS,
            )
            await _end_unjoined_call(ctx)

    watch_task = asyncio.create_task(watch(), name="openbase-participant-join-watch")

    async def _cancel_join_watch() -> None:
        room.off("participant_connected", on_participant_connected)
        watch_task.cancel()

    ctx.add_shutdown_callback(_cancel_join_watch)


async def _end_unjoined_call(ctx: JobContext) -> None:
    await _delete_room(str(getattr(ctx.room, "name", "") or ""))
    ctx.shutdown(reason="participant-join-timeout")


def requested_start_route(ctx: JobContext) -> VoiceRouteCommand | None:
    """The thread this call was started from, per the room-token dispatch metadata.

    A call started from a project thread talks to that thread; the phone passes
    the thread to ``/api/livekit-room-token/`` and the token view puts the
    prepared ``transfer_to_thread`` command under ``voice_route``. A call
    started from the dispatcher, an inbound call, or an older phone carries
    none and stays on the dispatcher.
    """
    raw = getattr(getattr(ctx, "job", None), "metadata", "") or ""
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.warning("Ignoring unreadable job metadata on room %s", ctx.room.name)
        return None
    if not isinstance(payload, dict):
        return None
    command = voice_route_command_from_payload(payload.get("voice_route"))
    if command is None or command.action != "transfer_to_thread":
        return None
    if not command.thread_id or not command.cwd:
        logger.warning(
            "Ignoring incomplete start route on room %s: thread_id=%s cwd=%s",
            ctx.room.name,
            command.thread_id or "",
            command.cwd or "",
        )
        return None
    return command


async def _apply_start_route(
    ctx: JobContext,
    voice_router: LiveKitVoiceRouter,
    *,
    prepare_task: "asyncio.Task[str]",
) -> str | None:
    """Route the call into the thread it was started from, before any speech.

    Returns None when the call is on its intended route, else the label of the
    thread that could not be reached (the call then stays on the dispatcher
    and the caller hears about it once the voice session is up).
    """
    route = requested_start_route(ctx)
    if route is None:
        return None
    label = route.label or route.active_target_voice_name or route.thread_id
    assert route.thread_id is not None and route.cwd is not None
    try:
        # The dispatcher client persists the route file; its thread id must be
        # known before the target route is written, or the write is skipped.
        await prepare_task
        await voice_router.transfer_to_thread(
            thread_id=route.thread_id,
            cwd=route.cwd,
            label=route.label,
            voice_id=route.active_target_voice_id,
            voice_name=route.active_target_voice_name,
        )
    except Exception:
        logger.warning(
            "Unable to start the call on thread %s in room %s; staying on the "
            "dispatcher",
            route.thread_id,
            ctx.room.name,
            exc_info=True,
        )
        voice_router.exit_to_dispatch()
        return str(label)
    logger.info(
        "dispatch_timing stage=call_started_on_thread room_name=%s thread_id=%s "
        "label=%s",
        ctx.room.name,
        route.thread_id,
        route.label or "",
    )
    return None


def _route_agent_label(voice_router: LiveKitVoiceRouter) -> str | None:
    """The name the live voice calls the active route, None for the dispatcher."""
    if voice_router.is_dispatcher_active:
        return None
    client = voice_router.active_client
    return (
        voice_router.active_target_voice_name
        or getattr(client, "_super_agent_name", None)
        or None
    )


def _announce_start_route_failure(
    session: AgentSession, live_bridge: LiveDelegationBridge | None, label: str
) -> None:
    text = f"I could not reach {label}, so you are talking to the dispatcher."
    try:
        if live_bridge is not None:
            live_bridge.announce(text)
        else:
            session.say(text)
    except Exception:
        logger.warning("Unable to announce the start route failure", exc_info=True)


def _wire_pipeline_voice_call(
    ctx: JobContext,
    session: AgentSession,
    announcer_tts: "VoiceSelectingTTS",
    voice_router: LiveKitVoiceRouter,
    delivery_ledger: VoiceDeliveryLedger,
    session_diagnostic_handlers: tuple,
    room_diagnostic_handlers: tuple,
) -> None:
    """Room and session plumbing for the pipeline engine (unchanged behaviour)."""
    delivery_ledger.set_user_speaking_provider(
        lambda: str(getattr(session, "user_state", "") or "") == "speaking"
    )

    def on_user_state_changed_for_mute(event) -> None:
        delivery_ledger.notify_user_state(
            new_state=str(getattr(event, "new_state", "") or ""),
            old_state=str(getattr(event, "old_state", "") or ""),
        )

    def on_final_transcript_for_mute(event) -> None:
        if (
            getattr(event, "is_final", False)
            and str(getattr(event, "transcript", "") or "").strip()
        ):
            delivery_ledger.notify_final_transcript()

    session.on("user_input_transcribed", on_final_transcript_for_mute)
    session.on("user_state_changed", on_user_state_changed_for_mute)
    set_vad_backlog_listener(delivery_ledger.notify_vad_gap)

    subscription_check_task = (
        asyncio.create_task(_verify_cloud_audio_subscription(ctx.room, session))
        if _uses_openbase_cloud_audio()
        else None
    )

    announcer_queue = AnnouncerSpeechQueue(
        session=session,
        announcer_tts=announcer_tts,
        delivery_ledger=delivery_ledger,
    )
    from .transcription_notice import TranscriptionTimeoutNotice

    transcription_notice = TranscriptionTimeoutNotice(announcer_queue)
    delivery_ledger.set_transcript_timeout_sink(transcription_notice.timed_out)
    delivery_ledger.set_announcement_pending_provider(
        announcer_queue.has_pending_announcements
    )

    announcer_queue_session_handlers = (
        ("user_state_changed", announcer_queue.notify_state_changed),
        ("agent_state_changed", announcer_queue.notify_state_changed),
        ("speech_created", announcer_queue.notify_state_changed),
    )
    for event_name, handler in announcer_queue_session_handlers:
        session.on(event_name, handler)

    announcer_queue.start()

    _register_orphaned_result_delivery(session, voice_router)
    _register_answer_owed_state_hold(session, voice_router)

    def on_data_received(data_packet: rtc.DataPacket) -> None:
        logger.info(
            "dispatch_timing stage=livekit_data_received topic=%s kind=%s "
            "payload_bytes=%d payload_hash=%s participant_identity=%s",
            data_packet.topic,
            data_packet.kind,
            len(data_packet.data),
            _packet_hash(data_packet),
            _packet_participant_identity(data_packet),
        )
        message = parse_announcer_packet(data_packet)
        if message is not None:
            logger.info(
                "dispatch_timing stage=announcer_packet_received message_id=%s "
                "voice_id=%s text_len=%d payload_hash=%s",
                message.message_id,
                message.voice_id or "",
                len(message.text),
                _packet_hash(data_packet),
            )
            announcer_queue.enqueue(message)
            return

        audio_message = parse_announcer_audio_packet(data_packet)
        if audio_message is not None:
            logger.info(
                "dispatch_timing stage=announcer_audio_packet_received "
                "message_id=%s audio_path=%s payload_hash=%s",
                audio_message.message_id,
                audio_message.audio_path,
                _packet_hash(data_packet),
            )
            announcer_queue.enqueue(audio_message)
            return

        route_command = parse_voice_route_packet(data_packet)
        if route_command is None:
            logger.info(
                "dispatch_timing stage=livekit_data_ignored topic=%s payload_hash=%s",
                data_packet.topic,
                _packet_hash(data_packet),
            )
            return
        logger.info(
            "dispatch_timing stage=voice_route_packet_received action=%s "
            "thread_id=%s cwd=%s label=%s active_target_voice_id=%s "
            "payload_hash=%s",
            route_command.action,
            route_command.thread_id or "",
            route_command.cwd or "",
            route_command.label or "",
            route_command.active_target_voice_id or "",
            _packet_hash(data_packet),
        )
        if route_command.action == "exit_to_dispatch":
            if voice_router.exit_to_dispatch():
                # Spoken by the Dispatcher's own voice, as when the Dispatcher
                # says it itself (2026-10-10: the announcer default voice said it).
                announcer_queue.enqueue(
                    AnnouncerMessage(
                        message_id=f"voice-route-{uuid.uuid4().hex}",
                        text="Back to dispatch.",
                        voice_id=_dispatcher_voice_id(),
                    )
                )
        elif route_command.action == "transfer_to_thread":
            if not route_command.thread_id or not route_command.cwd:
                logger.warning(
                    "Ignoring incomplete LiveKit voice route transfer command"
                )
                return
            asyncio.create_task(
                _transfer_voice_route(
                    voice_router,
                    route_command,
                    announcer_queue,
                )
            )
        else:
            logger.warning(
                "Ignoring unsupported LiveKit voice route action %s",
                route_command.action,
            )

    ctx.room.on("data_received", on_data_received)

    async def close_announcer_queue(*_args) -> None:
        if subscription_check_task is not None:
            subscription_check_task.cancel()
        ctx.room.off("data_received", on_data_received)
        for event_name, handler in room_diagnostic_handlers:
            ctx.room.off(event_name, handler)
        for event_name, handler in session_diagnostic_handlers:
            session.off(event_name, handler)
        for event_name, handler in announcer_queue_session_handlers:
            session.off(event_name, handler)
        session.off("user_input_transcribed", on_final_transcript_for_mute)
        session.off("user_state_changed", on_user_state_changed_for_mute)
        set_vad_backlog_listener(None)
        delivery_ledger.set_transcript_timeout_sink(None)
        await announcer_queue.close()
        await voice_router.close()

    ctx.add_shutdown_callback(close_announcer_queue)


def main():
    install_worker_failure_watchdog()
    install_proc_pool_liveness_patch()
    install_assemblyai_idle_noise_filter()
    install_vad_backlog_patch()
    install_gpt_live_reconnect_patch()
    cli.run_app(server)


def _log_prepare_result(task: asyncio.Task[str]) -> None:
    try:
        thread_id = task.result()
    except Exception:
        logger.warning("Failed to warm Codex LiveKit thread", exc_info=True)
    else:
        logger.info("Warmed Codex LiveKit thread %s", thread_id)


if __name__ == "__main__":
    main()
