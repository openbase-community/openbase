"""LiveKit agent entrypoint: server wiring for the Openbase Coder voice session.

The per-concern implementations live in sibling modules (``config``,
``voices``, ``spoken_commands``, ``codex_llm``, ``audio_scoring``,
``audio_diagnostics``, ``tts_selection``, ``packets``, ``speech_queue``,
``voice_routing``, ``room_diagnostics``, ``session_diagnostics``). Their
public names are re-exported here for backward compatibility.
"""

import asyncio
import inspect
import logging
import os
import uuid
from pathlib import Path

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
    LIVEKIT_AGENT_HOST,
    LIVEKIT_AGENT_LOAD_THRESHOLD_ENV,
    LIVEKIT_AGENT_NUM_IDLE_PROCESSES_ENV,
    LIVEKIT_AGENT_PORT,
    LIVEKIT_AUDIO_FRAME_LOG_EVERY,
    LIVEKIT_AUDIO_FRAME_LOG_FIRST,
    LIVEKIT_CODEX_ACK_DELAY_SECONDS,
    LIVEKIT_CODEX_ACK_MESSAGE,
    LIVEKIT_CODEX_APPROVAL_POLICY,
    LIVEKIT_CODEX_FRESH_THREAD_PER_SESSION,
    LIVEKIT_CODEX_SANDBOX,
    LIVEKIT_CODEX_THREAD_CWD,
    LIVEKIT_CODEX_THREAD_STATE_PATH,
    LIVEKIT_DISPATCH_AGENT_NAME,
    LIVEKIT_DISPATCHER_CONFIG_PATH,
    LIVEKIT_STT_PROVIDER,
    LIVEKIT_VERBOSE_LOGGING,
    OPENBASE_CLOUD_AUDIO_BASE_URL,
    OPENBASE_CLOUD_AUDIO_CARTESIA_VERSION,
    PROACTIVE_STEER_PROMPT_CACHE_SECONDS,
    SUPPORTED_AUDIO_EXTENSIONS,
    VOICE_ROUTE_TOPIC,
    WEB_BACKEND_URL,
    _canonical_env_path,
    _load_dispatcher_developer_instructions,
    _load_openbase_env,
    _optional_float_env,
    _optional_int_env,
    _read_instruction_file,
    load_direct_livekit_developer_instructions,
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
    publish_voice_lifecycle_packet,
)
from openbase_coder_cli.livekit_agent.proc_pool_patch import (
    install_proc_pool_liveness_patch,
)
from openbase_coder_cli.livekit_agent.room_diagnostics import (  # noqa: F401
    _participant_log_fields,
    _register_room_diagnostics,
    _track_log_fields,
)
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
    OPENBASE_CLOUD_STT_PROVIDER_ID,
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


def _uses_local_voice_model() -> bool:
    return (
        selected_stt_provider_id() == LOCAL_MLX_WHISPER_STT_PROVIDER_ID
        or selected_tts_provider_id() == KOKORO_PROVIDER_ID
    )


def _livekit_agent_server_options() -> dict[str, float | int]:
    uses_local_model = _uses_local_voice_model()
    options: dict[str, float | int] = {}

    load_threshold = _optional_float_env(LIVEKIT_AGENT_LOAD_THRESHOLD_ENV)
    if load_threshold is not None:
        options["load_threshold"] = load_threshold
    elif uses_local_model:
        options["load_threshold"] = float("inf")

    num_idle_processes = _optional_int_env(LIVEKIT_AGENT_NUM_IDLE_PROCESSES_ENV)
    if num_idle_processes is not None:
        options["num_idle_processes"] = num_idle_processes
    elif uses_local_model:
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
    vad_model = silero.VAD.load()
    proc.userdata["vad"] = (
        LoggingVAD(vad_model) if LIVEKIT_VERBOSE_LOGGING else vad_model
    )


server.setup_fnc = prewarm


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
        stt = assemblyai.STT(
            api_key=_openbase_cloud_audio_token(),
            base_url=_openbase_cloud_audio_ws_base_url("assemblyai"),
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
    if isinstance(
        exc,
        OpenbaseCloudAudioSubscriptionError
        | OpenbaseCloudAudioAuthenticationError
        | AuthLoginRequiredError
        | AuthTransientError,
    ):
        return str(exc)
    if _is_openbase_cloud_audio_authorization_error(exc):
        return (
            "Openbase Cloud audio authorization failed while starting this call. "
            "Sign in to Openbase Cloud again on your computer, then restart the "
            "Openbase Coder services or switch voice settings to direct provider "
            "keys or local audio."
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


async def _report_agent_error(room: rtc.Room, exc: Exception) -> None:
    """Best-effort: tell room participants why the agent cannot operate."""
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
    room_name = str(getattr(ctx.room, "name", "") or "")
    api_key = os.environ.get("LIVEKIT_API_KEY")
    api_secret = os.environ.get("LIVEKIT_API_SECRET")
    if room_name and api_key and api_secret:
        import livekit.api as livekit_api

        try:
            client = livekit_api.LiveKitAPI(
                url=os.environ.get("LIVEKIT_URL", "ws://localhost:7880"),
                api_key=api_key,
                api_secret=api_secret,
            )
            try:
                await client.room.delete_room(
                    livekit_api.DeleteRoomRequest(room=room_name)
                )
            finally:
                await client.aclose()
        except Exception:
            # Fall through to shutdown: the agent still leaves, and LiveKit's
            # empty-room timeout eventually ends the call.
            logger.exception("Unable to delete LiveKit room %s", room_name)
    ctx.shutdown(reason="agent-error")


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
            session.say(
                "Openbase Cloud audio is unavailable for this call. "
                "Check your Openbase subscription or voice settings."
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
    dispatcher_voice = dispatcher_voice_config()
    tts_provider = get_tts_provider(dispatcher_voice.provider)
    announcer_voice = (
        tts_provider.voice_for_id(CARTESIA_ANNOUNCER_VOICE_ID)
        if tts_provider.provider_id == CARTESIA_PROVIDER_ID
        else None
    ) or tts_provider.default_announcer_voice()
    openbase_cloud_audio_token = (
        _openbase_cloud_audio_token()
        if tts_provider.provider_id == OPENBASE_CLOUD_TTS_PROVIDER_ID
        else ""
    )
    cartesia_api_key = openbase_cloud_audio_token or CARTESIA_API_KEY
    cartesia_base_url = (
        _openbase_cloud_audio_http_base_url("cartesia")
        if openbase_cloud_audio_token
        else None
    )
    cartesia_api_version = (
        OPENBASE_CLOUD_AUDIO_CARTESIA_VERSION if openbase_cloud_audio_token else None
    )
    # Cloud audio tokens are short-lived; hand the TTS a way to refresh them
    # so websocket reconnects later in the session stay authenticated.
    audio_api_key_provider = (
        _openbase_cloud_audio_token if openbase_cloud_audio_token else None
    )
    direct_tts = VoiceSelectingTTS(
        default_voice_id=dispatcher_voice.voice_id,
        default_voice_name=dispatcher_voice.name,
        active_voice_id=lambda: voice_router.active_target_voice_id,
        active_voice_name=lambda: voice_router.active_target_voice_name,
        api_key=cartesia_api_key,
        api_key_provider=audio_api_key_provider,
        provider=tts_provider,
        role="direct",
        base_url=cartesia_base_url,
        api_version=cartesia_api_version,
        delivery_ledger=delivery_ledger,
    )
    announcer_tts = VoiceSelectingTTS(
        default_voice_id=announcer_voice.id,
        default_voice_name=announcer_voice.name,
        active_voice_id=lambda: voice_router.active_target_voice_id,
        active_voice_name=lambda: voice_router.active_target_voice_name,
        api_key=cartesia_api_key,
        api_key_provider=audio_api_key_provider,
        provider=tts_provider,
        role="announcer",
        base_url=cartesia_base_url,
        api_version=cartesia_api_version,
    )

    session_vad = _diagnostic_vad(ctx.proc.userdata["vad"])
    turn_signal_tracker = VoiceTurnSignalTracker()

    # Set up a voice AI pipeline
    session = AgentSession(
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
        },
        vad=session_vad,
        preemptive_generation=False,
    )
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


@server.rtc_session(agent_name=LIVEKIT_DISPATCH_AGENT_NAME)
async def livekit_agent(ctx: JobContext):
    _refresh_audio_credentials()
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }
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
    voice_router = LiveKitVoiceRouter(voice_backend_client)

    logger.info("Connecting to LiveKit room")
    try:
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    except Exception:
        logger.error(
            "LiveKit agent failed to connect to room %s; participants will "
            "stay at 'waiting for agent'",
            ctx.room.name,
            exc_info=True,
        )
        raise
    logger.info("Connected to LiveKit room")
    room_diagnostic_handlers = (
        _register_room_diagnostics(ctx.room) if LIVEKIT_VERBOSE_LOGGING else ()
    )
    delivery_ledger = VoiceDeliveryLedger(
        route_snapshot=voice_router.route_snapshot,
        room_name=ctx.room.name,
        room_id=await _room_sid(ctx.room),
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
    delivery_ledger.set_user_speaking_provider(
        lambda: str(getattr(session, "user_state", "") or "") == "speaking"
    )

    def on_user_state_changed_for_mute(event) -> None:
        delivery_ledger.notify_user_state(
            new_state=str(getattr(event, "new_state", "") or ""),
            old_state=str(getattr(event, "old_state", "") or ""),
        )

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
            voice_router.exit_to_dispatch()
            announcer_queue.enqueue(
                AnnouncerMessage(
                    message_id=f"voice-route-{uuid.uuid4().hex}",
                    text="Back to dispatch.",
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
        session.off("user_state_changed", on_user_state_changed_for_mute)
        set_vad_backlog_listener(None)
        await announcer_queue.close()
        await voice_router.close()

    ctx.add_shutdown_callback(close_announcer_queue)
    logger.info("LiveKit AgentSession started")


def main():
    install_worker_failure_watchdog()
    install_proc_pool_liveness_patch()
    install_assemblyai_idle_noise_filter()
    install_vad_backlog_patch()
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
