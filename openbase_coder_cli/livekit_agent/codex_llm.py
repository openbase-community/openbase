"""LiveKit LLM bridge that runs user turns against the Codex app-server."""

import asyncio
import hashlib
import logging
import uuid
from typing import TYPE_CHECKING

from livekit.agents import llm
from livekit.agents.llm.chat_context import ChatMessage
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from openbase_coder_cli.livekit_agent.config import (
    load_direct_livekit_developer_instructions,
)
from openbase_coder_cli.livekit_agent.spoken_commands import (
    _is_exit_to_dispatch_command,
)
from openbase_coder_cli.livekit_agent.tts_selection import text_for_tts
from openbase_coder_cli.livekit_agent.turn_detection import (
    VoiceTurnSignalTracker,
    decide_user_turn_closure,
    latest_user_turn_signals_from_chat_ctx,
)
from openbase_coder_cli.onboarding_reminder import append_onboarding_reminder

if TYPE_CHECKING:
    from openbase_coder_cli.livekit_agent.voice_routing import LiveKitVoiceRouter

logger = logging.getLogger(__name__)


class CodexLLMStream(llm.LLMStream):
    """Bridge a LiveKit user turn to the shared Codex app-server thread."""

    def __init__(
        self,
        livekit_llm: "CodexLiveKitLLM",
        *,
        chat_ctx,
        tools,
        conn_options,
    ) -> None:
        super().__init__(
            livekit_llm,
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
        )
        self._message_id = f"codex-{uuid.uuid4()}"
        self._voice_router = livekit_llm.voice_router
        self._turn_signal_tracker = livekit_llm.turn_signal_tracker

    def _latest_user_text(self) -> str:
        for item in reversed(self._chat_ctx.items):
            if isinstance(item, ChatMessage) and item.role == "user":
                return item.text_content or ""
        return ""

    async def _run(self) -> None:
        prompt = self._latest_user_text().strip()
        if not prompt:
            logger.info(
                "dispatch_timing stage=livekit_llm_empty_prompt message_id=%s",
                self._message_id,
            )
            return
        if self._voice_router.should_skip_proactively_steered_prompt(prompt):
            logger.info(
                "dispatch_timing stage=livekit_llm_prompt_already_steered "
                "message_id=%s prompt_len=%d prompt_hash=%s",
                self._message_id,
                len(prompt),
                hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12],
            )
            return
        logger.info(
            "dispatch_timing stage=livekit_llm_turn_start message_id=%s "
            "prompt_len=%d prompt_hash=%s active_thread_id=%s active_voice_id=%s",
            self._message_id,
            len(prompt),
            hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12],
            getattr(self._voice_router.active_client, "_thread_id", "") or "",
            self._voice_router.active_target_voice_id or "",
        )
        delivery_record = None
        delivery_ledger = self._voice_router.delivery_ledger
        turn_signals = latest_user_turn_signals_from_chat_ctx(
            self._chat_ctx,
            turn_signal_tracker=self._turn_signal_tracker,
        )
        if delivery_ledger is not None:
            delivery_record = delivery_ledger.accept_utterance(
                message_id=self._message_id,
                prompt=prompt,
            )
            delivery_ledger.schedule_user_turn_closure(
                delivery_record,
                decide_user_turn_closure(signals=turn_signals),
                signals=turn_signals,
            )

        try:
            await self._run_accepted_prompt(prompt, delivery_record, delivery_ledger)
        except asyncio.CancelledError:
            if delivery_record is not None:
                delivery_ledger.mark_cancelled(
                    delivery_record,
                    reason="livekit_llm_stream_cancelled",
                )
            raise
        except Exception:
            if delivery_record is not None:
                delivery_ledger.mark_cancelled(
                    delivery_record,
                    reason="livekit_llm_stream_failed",
                )
            raise

    async def _run_accepted_prompt(
        self,
        prompt: str,
        delivery_record,
        delivery_ledger,
    ) -> None:
        # When the dispatcher is already active, "exit to dispatch" is a no-op;
        # treat the utterance as a normal prompt instead of swallowing it.
        if (
            _is_exit_to_dispatch_command(prompt)
            and not self._voice_router.is_dispatcher_active
        ):
            self._voice_router.exit_to_dispatch()
            if delivery_record is not None:
                delivery_ledger.mark_cancelled(
                    delivery_record,
                    reason="exit_to_dispatch_command",
                )
            self._emit_delta("Back to dispatch.")
            return

        if self._voice_router.is_dispatcher_active:
            prompt = append_onboarding_reminder(prompt)

        voice_client = self._voice_router.active_client
        result = await voice_client.run_turn(
            prompt,
            developer_instructions=load_direct_livekit_developer_instructions(),
        )

        speech_text = result.get("_livekit_speech_text", "")
        turn_id = result.get("_livekit_turn_id", "")
        if delivery_record is not None and turn_id:
            delivery_ledger.mark_answer_owed(
                delivery_record,
                turn_id=turn_id,
                client=voice_client,
            )
        logger.info(
            "dispatch_timing stage=livekit_llm_turn_result message_id=%s "
            "turn_id=%s speech_len=%d speech_hash=%s event_channel_closed=%s",
            self._message_id,
            turn_id,
            len(speech_text),
            hashlib.sha256(speech_text.encode("utf-8")).hexdigest()[:12]
            if speech_text
            else "",
            self._event_ch.closed,
        )
        if speech_text and turn_id and not self._event_ch.closed:
            if delivery_record is not None:
                if not await delivery_ledger.wait_for_user_turn_closed_before_tts(
                    delivery_record
                ):
                    logger.info(
                        "dispatch_timing stage=livekit_llm_suppressed_before_user_turn_closed "
                        "message_id=%s turn_id=%s speech_len=%d",
                        self._message_id,
                        turn_id,
                        len(speech_text),
                    )
                    return
                if not delivery_ledger.mark_text_generated(
                    delivery_record,
                    speech_text=speech_text,
                    tts_text=text_for_tts(speech_text),
                ):
                    logger.info(
                        "dispatch_timing stage=livekit_llm_suppressed_terminal_delivery "
                        "message_id=%s turn_id=%s speech_len=%d",
                        self._message_id,
                        turn_id,
                        len(speech_text),
                    )
                    should_emit = False
                else:
                    should_emit = delivery_ledger.reserve_for_tts(delivery_record)
            else:
                should_emit = self._voice_router.claim_speech(voice_client, turn_id)
            if should_emit:
                try:
                    self._emit_delta(speech_text)
                except Exception:
                    if delivery_record is None:
                        voice_client.release_speech_claim(turn_id)
                    raise
        elif speech_text and turn_id:
            # The generation's event channel closed before the answer could
            # be emitted; hand it to the orphaned-result handler so it is
            # still spoken instead of silently dropped.
            orphan_handler = getattr(voice_client, "orphaned_result_handler", None)
            if callable(orphan_handler):
                logger.info(
                    "dispatch_timing stage=livekit_llm_channel_closed_redelivery "
                    "message_id=%s turn_id=%s speech_len=%d",
                    self._message_id,
                    turn_id,
                    len(speech_text),
                )
                orphan_handler(voice_client, turn_id, speech_text)
            elif delivery_record is not None:
                delivery_ledger.mark_cancelled(
                    delivery_record,
                    reason="event_channel_closed_without_orphan_handler",
                )
        elif delivery_record is not None:
            delivery_ledger.mark_cancelled(
                delivery_record,
                reason="turn_completed_without_speech",
            )

    def _emit_delta(self, text: str) -> None:
        self._event_ch.send_nowait(
            llm.ChatChunk(
                id=self._message_id,
                delta=llm.ChoiceDelta(role="assistant", content=text),
            )
        )
        logger.info(
            "dispatch_timing stage=livekit_llm_delta_emitted message_id=%s "
            "text_len=%d text_hash=%s text_excerpt=%r",
            self._message_id,
            len(text),
            hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
            text[:160],
        )


class CodexLiveKitLLM(llm.LLM):
    """LiveKit LLM wrapper backed by a shared Codex app-server thread."""

    def __init__(
        self,
        voice_router: "LiveKitVoiceRouter",
        *,
        turn_signal_tracker: VoiceTurnSignalTracker | None = None,
    ) -> None:
        super().__init__()
        self.voice_router = voice_router
        self.turn_signal_tracker = turn_signal_tracker

    @property
    def model(self) -> str:
        return self.voice_router.active_client.model_name

    @property
    def provider(self) -> str:
        return "openai"

    def chat(
        self,
        *,
        chat_ctx,
        tools=None,
        conn_options=DEFAULT_API_CONNECT_OPTIONS,
        **kwargs,
    ) -> llm.LLMStream:
        return CodexLLMStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
        )
