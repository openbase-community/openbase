"""Agent session event handlers: diagnostics and proactive turn steering."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from livekit.agents import AgentSession

from openbase_coder_cli.livekit_agent.logging_utils import (
    _event_text_hash,
    exception_chain_summary,
)
from openbase_coder_cli.livekit_agent.spoken_commands import (
    _is_exit_to_dispatch_command,
)
from openbase_coder_cli.livekit_agent.text_normalization import normalize_spoken_text
from openbase_coder_cli.livekit_agent.voice_routing import LiveKitVoiceRouter
from openbase_coder_cli.voice_tags import wrap_voice_prompt

logger = logging.getLogger(__name__)

# When the framework's turn-completion lands while an uninterruptible speech
# (announcer intro, audio-file playback) is the current speech, it drops the
# reply entirely — the utterance never even reaches the chat context. A final
# transcript that was neither steered nor added as a conversation item within
# this grace window is treated as dropped and recovered once speech frees up.
DROPPED_UTTERANCE_GRACE_SECONDS = 3.0
DROPPED_UTTERANCE_SPEECH_WAIT_SECONDS = 30.0


def _register_session_diagnostics(
    session: AgentSession,
    voice_router: LiveKitVoiceRouter,
    *,
    enable_logging: bool,
    on_unrecoverable_error: Callable[[Exception], Awaitable[None]] | None = None,
):
    proactive_steer_tasks: set[asyncio.Task[None]] = set()
    recovery_tasks: set[asyncio.Task[None]] = set()
    # normalized final transcript -> raw transcript, pending until steered,
    # observed as a user conversation item, or recovered.
    pending_final_transcripts: dict[str, str] = {}
    error_reported = False

    def _mark_transcript_handled(text: str) -> None:
        if not pending_final_transcripts:
            return
        normalized = normalize_spoken_text(text)
        if not normalized:
            return
        for key in list(pending_final_transcripts):
            if key in normalized:
                pending_final_transcripts.pop(key, None)

    async def _recover_dropped_utterance(key: str) -> None:
        await asyncio.sleep(DROPPED_UTTERANCE_GRACE_SECONDS)
        if key not in pending_final_transcripts:
            return
        # Wait out the uninterruptible speech that swallowed the reply.
        waited = 0.0
        while waited < DROPPED_UTTERANCE_SPEECH_WAIT_SECONDS:
            speech = getattr(session, "current_speech", None)
            if speech is None or speech.allow_interruptions:
                break
            await asyncio.sleep(0.25)
            waited += 0.25
        if key not in pending_final_transcripts:
            return
        # Fold every still-pending fragment into one recovery so a thought
        # split across finals comes back as a single reply, not several.
        transcript = " ".join(pending_final_transcripts.values())
        pending_final_transcripts.clear()
        logger.warning(
            "dispatch_timing stage=session_dropped_utterance_recovered "
            "transcript_len=%d transcript_hash=%s waited_ms=%d",
            len(transcript),
            _event_text_hash(transcript),
            int(waited * 1000),
        )
        try:
            session.generate_reply(user_input=transcript)
        except Exception:
            logger.warning(
                "dispatch_timing stage=session_dropped_utterance_recovery_failed "
                "transcript_hash=%s",
                _event_text_hash(transcript),
                exc_info=True,
            )

    def _watch_for_dropped_utterance(transcript: str) -> None:
        key = normalize_spoken_text(transcript)
        if not key:
            return
        pending_final_transcripts[key] = transcript
        task = asyncio.create_task(
            _recover_dropped_utterance(key),
            name="openbase-dropped-utterance-recovery",
        )
        recovery_tasks.add(task)
        task.add_done_callback(recovery_tasks.discard)

    async def proactively_steer_final_transcript(transcript: str) -> None:
        try:
            steer_active_turn = getattr(
                voice_router.active_client,
                "steer_active_turn",
                None,
            )
            if not callable(steer_active_turn):
                return
            turn_id = await steer_active_turn(wrap_voice_prompt(transcript))
            if not turn_id:
                return
            _mark_transcript_handled(transcript)
            voice_router.mark_proactive_steer(transcript)
            delivery_ledger = voice_router.delivery_ledger
            if delivery_ledger is not None:
                # A steered message is received input: give the user the same
                # mute-as-receipt the accepted-utterance path provides.
                delivery_ledger.schedule_steer_receipt_closure()
            logger.info(
                "dispatch_timing stage=session_user_input_proactive_steer "
                "turn_id=%s transcript_len=%d transcript_hash=%s",
                turn_id,
                len(transcript),
                _event_text_hash(transcript),
            )
        except Exception:
            logger.warning(
                "dispatch_timing stage=session_user_input_proactive_steer_failed "
                "transcript_len=%d transcript_hash=%s",
                len(transcript),
                _event_text_hash(transcript),
                exc_info=True,
            )

    def schedule_proactive_steer(transcript: str) -> None:
        if _is_exit_to_dispatch_command(transcript):
            return
        task = asyncio.create_task(
            proactively_steer_final_transcript(transcript),
            name="openbase-proactive-super-agents-steer",
        )
        proactive_steer_tasks.add(task)
        task.add_done_callback(proactive_steer_tasks.discard)

    def on_user_state_changed(event) -> None:
        if not enable_logging:
            return
        logger.info(
            "dispatch_timing stage=session_user_state_changed old_state=%s new_state=%s",
            getattr(event, "old_state", ""),
            getattr(event, "new_state", ""),
        )

    def on_agent_state_changed(event) -> None:
        if not enable_logging:
            return
        logger.info(
            "dispatch_timing stage=session_agent_state_changed old_state=%s new_state=%s",
            getattr(event, "old_state", ""),
            getattr(event, "new_state", ""),
        )

    def on_user_input_transcribed(event) -> None:
        transcript = str(getattr(event, "transcript", "") or "")
        is_final = str(getattr(event, "is_final", "")).lower() == "true"
        if enable_logging:
            logger.info(
                "dispatch_timing stage=session_user_input_transcribed final=%s "
                "speaker_id=%s language=%s transcript_len=%d transcript_hash=%s "
                "transcript_excerpt=%r",
                getattr(event, "is_final", ""),
                getattr(event, "speaker_id", "") or "",
                getattr(event, "language", "") or "",
                len(transcript),
                _event_text_hash(transcript),
                transcript[:160],
            )
        if is_final and transcript.strip():
            stripped = transcript.strip()
            if not _is_exit_to_dispatch_command(stripped):
                _watch_for_dropped_utterance(stripped)
            schedule_proactive_steer(stripped)

    def on_conversation_item_added(event) -> None:
        item = getattr(event, "item", None)
        text_content = str(getattr(item, "text_content", "") or "")
        if getattr(item, "role", "") == "user":
            # The utterance made it into the chat context; the framework did
            # not drop it, so cancel any pending dropped-utterance recovery.
            _mark_transcript_handled(text_content)
        if not enable_logging:
            return
        logger.info(
            "dispatch_timing stage=session_conversation_item_added item_type=%s "
            "role=%s text_len=%d text_hash=%s text_excerpt=%r",
            type(item).__name__,
            getattr(item, "role", "") or "",
            len(text_content),
            _event_text_hash(text_content),
            text_content[:160],
        )

    def on_speech_created(event) -> None:
        if not enable_logging:
            return
        speech_handle = getattr(event, "speech_handle", None)
        logger.info(
            "dispatch_timing stage=session_speech_created user_initiated=%s "
            "source=%s speech_handle_id=%s",
            getattr(event, "user_initiated", ""),
            getattr(event, "source", ""),
            getattr(speech_handle, "id", "") or getattr(speech_handle, "_id", ""),
        )

    def on_error(event) -> None:
        nonlocal error_reported
        error = getattr(event, "error", None)
        if enable_logging:
            logger.warning(
                "dispatch_timing stage=session_error source=%s error_type=%s error=%s",
                type(getattr(event, "source", None)).__name__,
                type(error).__name__,
                exception_chain_summary(error)
                if isinstance(error, Exception)
                else str(error),
            )
        if (
            not error_reported
            and on_unrecoverable_error is not None
            and isinstance(error, Exception)
        ):
            error_reported = True
            asyncio.create_task(on_unrecoverable_error(error))

    def on_close(event) -> None:
        nonlocal error_reported
        error = getattr(event, "error", None)
        if enable_logging:
            logger.info(
                "dispatch_timing stage=session_close reason=%s error_type=%s error=%s",
                getattr(event, "reason", ""),
                type(error).__name__,
                exception_chain_summary(error)
                if isinstance(error, Exception)
                else error,
            )
        if (
            not error_reported
            and on_unrecoverable_error is not None
            and isinstance(error, Exception)
        ):
            error_reported = True
            asyncio.create_task(on_unrecoverable_error(error))

    handlers = (
        ("user_state_changed", on_user_state_changed),
        ("agent_state_changed", on_agent_state_changed),
        ("user_input_transcribed", on_user_input_transcribed),
        ("conversation_item_added", on_conversation_item_added),
        ("speech_created", on_speech_created),
        ("error", on_error),
        ("close", on_close),
    )
    for event_name, handler in handlers:
        session.on(event_name, handler)
    return handlers
