"""Voice delivery state and diagnostics for LiveKit spoken turns."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from openbase_coder_cli.livekit_agent.turn_detection import (
    MAX_PRE_ACCEPT_QUIET_CREDIT_SECONDS,
    VAD_ONLY_USER_TURN_QUIET_GRACE_SECONDS,
    UserTurnClosureDecision,
    UserTurnClosureSignals,
)

logger = logging.getLogger(__name__)

# STT finals arriving later than this after end of speech dominate perceived
# mute latency (the quiet floor cannot start until the turn is accepted).
# Surface it loudly so slow STT reads as an infrastructure problem, not as
# "the app feels sluggish".
SLOW_TRANSCRIPTION_WARN_SECONDS = 2.0


def short_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class VoiceRouteSnapshot:
    route_version: int
    active_thread_id: str
    active_voice_id: str | None
    active_voice_name: str | None
    active_route: str

    def same_route(self, other: "VoiceRouteSnapshot | None") -> bool:
        return (
            other is not None
            and self.route_version == other.route_version
            and self.active_thread_id == other.active_thread_id
            and self.active_route == other.active_route
        )


@dataclass
class VoiceDeliveryRecord:
    delivery_id: str
    message_id: str
    prompt_hash: str
    prompt_len: int
    room_name: str
    room_id: str
    route_at_acceptance: VoiceRouteSnapshot
    accepted_at: float = field(default_factory=time.monotonic)
    status: str = "utterance_accepted"
    turn_id: str | None = None
    client: Any = field(default=None, repr=False)
    route_at_text_generation: VoiceRouteSnapshot | None = None
    speech_hash: str = ""
    speech_len: int = 0
    tts_text: str = field(default="", repr=False)
    tts_text_hash: str = ""
    tts_text_len: int = 0
    route_at_tts_flush: VoiceRouteSnapshot | None = None
    first_audio_latency_ms: int | None = None
    audio_events: int = 0
    audio_seconds: float = 0.0
    reserved_for_tts: bool = False
    delivered: bool = False
    terminal_reason: str | None = None
    user_turn_closed: bool = False
    user_turn_closure_confidence: float | None = None
    user_turn_closure_source: str = ""
    user_turn_closure_delay_ms: int | None = None
    user_turn_completion_reason: str = ""
    user_turn_eou_probability: float | None = None
    user_turn_silence_ms: int | None = None
    user_turn_transcript_confidence: float | None = None
    user_turn_transcription_delay_ms: int | None = None


class VoiceDeliveryLedger:
    """Single source of truth for spoken-turn delivery state."""

    def __init__(
        self,
        *,
        route_snapshot: Callable[[], VoiceRouteSnapshot],
        room_name: str = "",
        room_id: str = "",
        user_speaking_poll_seconds: float = 0.1,
        vad_quiet_grace_seconds: float = VAD_ONLY_USER_TURN_QUIET_GRACE_SECONDS,
    ) -> None:
        self._route_snapshot = route_snapshot
        self._room_name = room_name
        self._room_id = room_id
        self._user_speaking_poll_seconds = user_speaking_poll_seconds
        self._vad_quiet_grace_seconds = vad_quiet_grace_seconds
        self._vad_quiet_task: asyncio.Task[None] | None = None
        # True while a safe_to_mute_user has been emitted and neither renewed
        # user speech nor a safe_to_unmute has reopened the mic since.
        self._mute_covers_current_quiet = False
        # When the VAD input backlog is dropped, skipped audio may have
        # contained speech: quiet observed before this instant cannot count
        # toward a mute.
        self._last_vad_gap_at = float("-inf")
        self._records: dict[str, VoiceDeliveryRecord] = {}
        self._records_by_turn_id: dict[str, VoiceDeliveryRecord] = {}
        self._pending_by_tts_hash: dict[str, deque[str]] = {}
        self._lifecycle_sink: (
            Callable[
                [str, VoiceDeliveryRecord, str],
                None,
            ]
            | None
        ) = None
        self._user_speaking_provider: Callable[[], bool] | None = None
        self._user_turn_closure_tasks: dict[str, asyncio.Task[None]] = {}

    def set_lifecycle_sink(
        self,
        sink: Callable[[str, VoiceDeliveryRecord, str], None] | None,
    ) -> None:
        self._lifecycle_sink = sink

    def set_user_speaking_provider(self, provider: Callable[[], bool] | None) -> None:
        self._user_speaking_provider = provider

    def notify_user_state(self, *, new_state: str, old_state: str = "") -> None:
        """Drive the STT-independent provisional mute from VAD user state.

        STT finals can arrive many seconds after end of speech on degraded
        links, and the transcript-informed closure cannot start until then.
        VAD end-of-speech is local and immediate, so a speaking→quiet
        transition starts a provisional quiet-floor timer that emits
        ``safe_to_mute_user`` before any transcript exists. Renewed speech
        cancels it; a transcript-informed closure supersedes it.
        """
        if new_state == "speaking":
            self._mute_covers_current_quiet = False
            self._cancel_vad_quiet_task()
            return
        if old_state == "speaking":
            self._start_vad_quiet_closure_task()

    def notify_vad_gap(self, dropped_seconds: float) -> None:
        """Restart quiet verification after the VAD backlog was dropped."""
        self._last_vad_gap_at = time.monotonic()
        logger.warning(
            "dispatch_timing stage=voice_delivery_vad_gap dropped_ms=%d room=%s",
            max(0, int(dropped_seconds * 1000)),
            self._room_name,
        )

    def _start_vad_quiet_closure_task(self) -> None:
        if self._mute_covers_current_quiet:
            return
        self._cancel_vad_quiet_task()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._vad_quiet_task = loop.create_task(
            self._vad_quiet_closure_after_floor(),
            name="openbase-voice-vad-quiet-closure",
        )

    def _cancel_vad_quiet_task(self) -> None:
        task = self._vad_quiet_task
        self._vad_quiet_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _vad_quiet_closure_after_floor(self) -> None:
        deadline = time.monotonic() + self._vad_quiet_grace_seconds
        while True:
            # A VAD backlog gap invalidates quiet observed before it; the
            # floor restarts from the gap.
            deadline = max(
                deadline, self._last_vad_gap_at + self._vad_quiet_grace_seconds
            )
            if self._mute_covers_current_quiet or self._user_is_speaking():
                return
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(self._user_speaking_poll_seconds)
        self._emit_vad_quiet_mute()

    def _emit_vad_quiet_mute(self) -> None:
        record = VoiceDeliveryRecord(
            delivery_id=f"voice-vad-{uuid.uuid4().hex[:12]}",
            message_id="",
            prompt_hash="",
            prompt_len=0,
            room_name=self._room_name,
            room_id=self._room_id,
            route_at_acceptance=self._route_snapshot(),
            status="vad_quiet_closure",
        )
        record.user_turn_closed = True
        record.user_turn_closure_source = "vad_quiet_floor"
        record.user_turn_closure_delay_ms = max(
            0, int(self._vad_quiet_grace_seconds * 1000)
        )
        record.user_turn_completion_reason = "vad_quiet_floor"
        self._records[record.delivery_id] = record
        self._log_user_turn_closure(record, "safe_to_mute_user")
        self._emit_lifecycle("safe_to_mute_user", record, reason="vad_quiet_floor")

    def accept_utterance(self, *, message_id: str, prompt: str) -> VoiceDeliveryRecord:
        self._cancel_superseded_pending_for_current_route()
        route = self._route_snapshot()
        record = VoiceDeliveryRecord(
            delivery_id=f"voice-delivery-{uuid.uuid4().hex[:12]}",
            message_id=message_id,
            prompt_hash=short_text_hash(prompt),
            prompt_len=len(prompt),
            room_name=self._room_name,
            room_id=self._room_id,
            route_at_acceptance=route,
        )
        self._records[record.delivery_id] = record
        self._log(record, "utterance_accepted")
        self._emit_lifecycle("utterance_accepted", record)
        return record

    def schedule_user_turn_closure(
        self,
        record: VoiceDeliveryRecord,
        decision: UserTurnClosureDecision,
        *,
        signals: UserTurnClosureSignals | None = None,
    ) -> None:
        """Emit ``safe_to_mute_user`` once the user stays quiet for the floor."""
        self._cancel_user_turn_closure_task(record.delivery_id)
        record.user_turn_closure_confidence = decision.confidence
        record.user_turn_closure_source = decision.source
        record.user_turn_closure_delay_ms = decision.quiet_grace_ms
        record.user_turn_completion_reason = decision.completion_reason
        self._apply_user_turn_signals(record, signals)
        # LiveKit endpointing already verified this much silence before the
        # utterance was accepted; the quiet floor counts it so the mute lands
        # relative to when the user actually stopped speaking.
        quiet_credit_seconds = 0.0
        if signals is not None and signals.end_of_turn_delay is not None:
            quiet_credit_seconds = min(
                max(signals.end_of_turn_delay, 0.0),
                MAX_PRE_ACCEPT_QUIET_CREDIT_SECONDS,
            )
        if (
            signals is not None
            and signals.transcription_delay is not None
            and signals.transcription_delay >= SLOW_TRANSCRIPTION_WARN_SECONDS
        ):
            logger.warning(
                "dispatch_timing stage=stt_transcription_delayed "
                "transcription_delay_ms=%d delivery_id=%s message_id=%s "
                "prompt_len=%d room=%s",
                int(signals.transcription_delay * 1000),
                record.delivery_id,
                record.message_id,
                record.prompt_len,
                record.room_name,
            )
        self._log_user_turn_closure(record, "user_turn_closure_decision")

        # The transcript-informed decision supersedes any pending VAD-only
        # provisional timer. If a provisional mute already fired during this
        # same quiet stretch, adopt it: the mic is already muted, so close
        # the record without re-emitting.
        self._cancel_vad_quiet_task()
        if self._mute_covers_current_quiet and not self._user_is_speaking():
            record.user_turn_closed = True
            self._log_user_turn_closure(
                record,
                "safe_to_mute_user_adopted_vad_mute",
                reason=decision.completion_reason,
            )
            return

        async def _close_after_delay() -> None:
            try:
                await self._wait_until_user_turn_closure_allowed(
                    record,
                    decision=decision,
                    initial_quiet_credit_seconds=quiet_credit_seconds,
                )
                self.mark_user_turn_closed(record, decision=decision)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "dispatch_timing stage=voice_delivery_user_turn_closure_failed "
                    "delivery_id=%s message_id=%s",
                    record.delivery_id,
                    record.message_id,
                    exc_info=True,
                )
            finally:
                current_task = asyncio.current_task()
                if (
                    self._user_turn_closure_tasks.get(record.delivery_id)
                    is current_task
                ):
                    self._user_turn_closure_tasks.pop(record.delivery_id, None)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if decision.quiet_grace_seconds <= 0:
                self.mark_user_turn_closed(record, decision=decision)
            else:
                logger.warning(
                    "dispatch_timing stage=voice_delivery_user_turn_closure_not_scheduled "
                    "delivery_id=%s message_id=%s reason=no_running_loop quiet_grace_ms=%d",
                    record.delivery_id,
                    record.message_id,
                    decision.quiet_grace_ms,
                )
            return

        self._user_turn_closure_tasks[record.delivery_id] = loop.create_task(
            _close_after_delay(),
            name="openbase-voice-user-turn-closure",
        )

    async def wait_for_user_turn_closed_before_tts(
        self,
        record: VoiceDeliveryRecord,
    ) -> bool:
        """Wait until the user turn is closed before allowing agent speech.

        The backend may produce text while the user is still adding to their
        thought. The lifecycle ledger is the authority for when the iOS app may
        mute and when the response can become audible.
        """
        if record.user_turn_closed:
            return True
        wait_logged = False
        wait_started = time.monotonic()
        while not record.user_turn_closed:
            if record.status in _TERMINAL_STATUSES or record.delivered:
                return False
            if self._is_stale(record):
                self.mark_suppressed_stale(
                    record,
                    reason="route_changed_before_user_turn_closed",
                )
                return False
            if not wait_logged:
                wait_logged = True
                self._log_user_turn_closure(
                    record,
                    "tts_waiting_for_user_turn_closed",
                )
            await asyncio.sleep(self._user_speaking_poll_seconds)
        if wait_logged:
            logger.info(
                "dispatch_timing stage=voice_delivery_tts_user_turn_closed_wait_done "
                "delivery_id=%s message_id=%s wait_ms=%d",
                record.delivery_id,
                record.message_id,
                int((time.monotonic() - wait_started) * 1000),
            )
        return True

    def mark_user_turn_closed(
        self,
        record: VoiceDeliveryRecord,
        *,
        decision: UserTurnClosureDecision,
    ) -> None:
        if record.user_turn_closed:
            return
        if record.status in _TERMINAL_STATUSES or record.delivered:
            self._log_user_turn_closure(
                record,
                "safe_to_mute_user_skipped",
                reason="terminal_or_delivered",
            )
            return
        if self._is_stale(record):
            self._log_user_turn_closure(
                record,
                "safe_to_mute_user_skipped",
                reason="stale_route",
            )
            return
        record.user_turn_closed = True
        record.user_turn_closure_confidence = decision.confidence
        record.user_turn_closure_source = decision.source
        record.user_turn_closure_delay_ms = decision.quiet_grace_ms
        record.user_turn_completion_reason = decision.completion_reason
        self._log_user_turn_closure(record, "safe_to_mute_user")
        self._emit_lifecycle(
            "safe_to_mute_user",
            record,
            reason=decision.completion_reason,
        )

    async def _wait_until_user_turn_closure_allowed(
        self,
        record: VoiceDeliveryRecord,
        *,
        decision: UserTurnClosureDecision,
        initial_quiet_credit_seconds: float = 0.0,
    ) -> None:
        """Wait for the user to stay quiet for the decision's full floor.

        Quiet time counts from utterance acceptance, backdated by any silence
        LiveKit endpointing already verified; resumed speech resets the
        clock. Without a speaking provider (tests, degraded sessions) the
        floor degrades to a plain sleep.
        """
        required_quiet_seconds = decision.quiet_grace_seconds
        if self._user_speaking_provider is None:
            await asyncio.sleep(
                max(0.0, required_quiet_seconds - initial_quiet_credit_seconds)
            )
            return
        quiet_started_at: float | None = time.monotonic() - initial_quiet_credit_seconds
        wait_logged = False
        wait_started = time.monotonic()
        while True:
            if record.status in _TERMINAL_STATUSES or record.delivered:
                return
            if not self._user_is_speaking():
                if quiet_started_at is None:
                    quiet_started_at = time.monotonic()
                # A VAD backlog gap invalidates quiet observed before it
                # (including pre-accept credit); restart the floor there.
                quiet_started_at = max(quiet_started_at, self._last_vad_gap_at)
                if time.monotonic() - quiet_started_at >= required_quiet_seconds:
                    if wait_logged:
                        logger.info(
                            "dispatch_timing stage=voice_delivery_user_speech_wait_done "
                            "delivery_id=%s message_id=%s wait_ms=%d",
                            record.delivery_id,
                            record.message_id,
                            int((time.monotonic() - wait_started) * 1000),
                        )
                    return
            else:
                quiet_started_at = None
                if not wait_logged:
                    wait_logged = True
                    self._log_user_turn_closure(
                        record,
                        "safe_to_mute_user_waiting_for_user_speech_end",
                        reason=decision.completion_reason,
                    )
            await asyncio.sleep(self._user_speaking_poll_seconds)

    def _user_is_speaking(self) -> bool:
        if self._user_speaking_provider is None:
            return False
        try:
            return bool(self._user_speaking_provider())
        except Exception:
            logger.warning(
                "dispatch_timing stage=voice_delivery_user_speaking_provider_failed",
                exc_info=True,
            )
            return False

    def mark_answer_owed(
        self,
        record: VoiceDeliveryRecord,
        *,
        turn_id: str,
        client: Any,
    ) -> bool:
        if record.status in _TERMINAL_STATUSES:
            self._log(record, "answer_owed_rejected", reason="terminal_record")
            return False
        record.turn_id = turn_id
        record.client = client
        record.status = "answer_owed"
        self._records_by_turn_id[turn_id] = record
        self._log(record, "answer_owed")
        return True

    def mark_text_generated(
        self,
        record: VoiceDeliveryRecord,
        *,
        speech_text: str,
        tts_text: str,
    ) -> bool:
        if record.status in _TERMINAL_STATUSES:
            self._log(record, "text_generated_rejected", reason="terminal_record")
            return False
        route = self._route_snapshot()
        record.status = "text_generated"
        record.route_at_text_generation = route
        record.speech_hash = short_text_hash(speech_text)
        record.speech_len = len(speech_text)
        record.tts_text = tts_text
        record.tts_text_hash = short_text_hash(tts_text)
        record.tts_text_len = len(tts_text)
        self._pending_by_tts_hash.setdefault(record.tts_text_hash, deque()).append(
            record.delivery_id
        )
        self._log(record, "text_generated")
        return True

    def reserve_for_tts(self, record: VoiceDeliveryRecord) -> bool:
        if record.status in _TERMINAL_STATUSES:
            self._log(record, "tts_reservation_rejected", reason="terminal_record")
            return False
        if self._is_stale(record):
            self.mark_suppressed_stale(record, reason="route_changed_before_emit")
            return False
        if record.reserved_for_tts or record.delivered:
            self._log(record, "tts_reservation_rejected", reason="already_reserved")
            return False
        record.reserved_for_tts = True
        self._log(record, "tts_reserved")
        return True

    def match_tts_flush(
        self,
        *,
        tts_text: str,
        role: str,
        voice_id: str | None,
        voice_name: str | None,
    ) -> VoiceDeliveryRecord | None:
        tts_hash = short_text_hash(tts_text)
        pending = self._pending_by_tts_hash.get(tts_hash)
        while pending:
            delivery_id = pending.popleft()
            record = self._records.get(delivery_id)
            if record and record.status not in _TERMINAL_STATUSES:
                return self._mark_tts_flushed_or_suppressed(
                    record,
                    role=role,
                    voice_id=voice_id,
                    voice_name=voice_name,
                )
        fallback_record = self._find_compatible_tts_fallback(tts_text)
        if fallback_record is not None:
            self._remove_pending_tts_hash(fallback_record)
            logger.info(
                "dispatch_timing stage=voice_delivery_tts_fallback_matched "
                "delivery_id=%s message_id=%s reserved_text_len=%d "
                "reserved_text_hash=%s actual_text_len=%d actual_text_hash=%s "
                "room=%s room_id=%s",
                fallback_record.delivery_id,
                fallback_record.message_id,
                fallback_record.tts_text_len,
                fallback_record.tts_text_hash,
                len(tts_text),
                tts_hash,
                self._room_name,
                self._room_id,
            )
            return self._mark_tts_flushed_or_suppressed(
                fallback_record,
                role=role,
                voice_id=voice_id,
                voice_name=voice_name,
            )
        logger.info(
            "dispatch_timing stage=voice_delivery_tts_unmatched role=%s "
            "voice_id=%s voice_name=%s text_len=%d text_hash=%s room=%s room_id=%s",
            role,
            voice_id or "",
            voice_name or "",
            len(tts_text),
            tts_hash,
            self._room_name,
            self._room_id,
        )
        return None

    def track_unmatched_tts(self, *, tts_text: str) -> VoiceDeliveryRecord:
        """Track direct agent speech that has no accepted-utterance record.

        Late steer responses and control phrases reach TTS without a matched
        delivery record. Clients still need ``agent_audio_started`` /
        ``agent_audio_finished`` / ``safe_to_unmute`` around that audio, or
        their unmute timing goes blind to it and the mic opens mid-speech.
        """
        record = VoiceDeliveryRecord(
            delivery_id=f"voice-direct-{uuid.uuid4().hex[:12]}",
            message_id="",
            prompt_hash="",
            prompt_len=0,
            room_name=self._room_name,
            room_id=self._room_id,
            route_at_acceptance=self._route_snapshot(),
            status="tts_flushed",
        )
        record.tts_text = tts_text
        record.tts_text_hash = short_text_hash(tts_text)
        record.tts_text_len = len(tts_text)
        record.reserved_for_tts = True
        self._records[record.delivery_id] = record
        self._log(record, "direct_tts_tracked")
        return record

    def _mark_tts_flushed_or_suppressed(
        self,
        record: VoiceDeliveryRecord,
        *,
        role: str,
        voice_id: str | None,
        voice_name: str | None,
    ) -> VoiceDeliveryRecord:
        if self._is_stale(record):
            self.mark_suppressed_stale(
                record,
                reason="route_changed_before_tts_flush",
                role=role,
                voice_id=voice_id,
                voice_name=voice_name,
            )
            return record
        record.status = "tts_flushed"
        record.route_at_tts_flush = self._route_snapshot()
        self._log(
            record,
            "tts_flushed",
            role=role,
            voice_id=voice_id,
            voice_name=voice_name,
        )
        return record

    def _find_compatible_tts_fallback(
        self,
        actual_tts_text: str,
    ) -> VoiceDeliveryRecord | None:
        """Recover when speech formatting changes text between reservation and flush."""
        current_route = self._route_snapshot()
        candidates = [
            record
            for record in self._records.values()
            if record.reserved_for_tts
            and not record.delivered
            and record.status not in _TERMINAL_STATUSES
            and record.route_at_acceptance.same_route(current_route)
        ]
        if len(candidates) != 1:
            return None
        candidate = candidates[0]
        if _tts_texts_compatible(candidate.tts_text, actual_tts_text):
            return candidate
        return None

    def _remove_pending_tts_hash(self, record: VoiceDeliveryRecord) -> None:
        pending = self._pending_by_tts_hash.get(record.tts_text_hash)
        if not pending:
            return
        try:
            pending.remove(record.delivery_id)
        except ValueError:
            return

    def mark_audio_started(
        self,
        record: VoiceDeliveryRecord,
        *,
        latency_ms: int,
        role: str,
        voice_id: str | None,
        voice_name: str | None,
    ) -> None:
        self._cancel_user_turn_closure_task(record.delivery_id)
        record.status = "audio_started"
        record.first_audio_latency_ms = latency_ms
        if record.turn_id and record.client is not None and not record.delivered:
            claim = getattr(record.client, "claim_speech", None)
            if callable(claim):
                claim(record.turn_id)
                record.delivered = True
            else:
                record.delivered = True
        else:
            record.delivered = True
        self._log(
            record,
            "audio_started",
            role=role,
            voice_id=voice_id,
            voice_name=voice_name,
        )
        self._emit_lifecycle("agent_audio_started", record)

    def mark_tts_completed(
        self,
        record: VoiceDeliveryRecord,
        *,
        audio_events: int,
        audio_seconds: float,
        role: str,
        voice_id: str | None,
        voice_name: str | None,
    ) -> None:
        self._cancel_user_turn_closure_task(record.delivery_id)
        record.audio_events = audio_events
        record.audio_seconds = audio_seconds
        if audio_events > 0:
            if not record.delivered and record.turn_id and record.client is not None:
                claim = getattr(record.client, "claim_speech", None)
                if callable(claim):
                    claim(record.turn_id)
                record.delivered = True
            record.status = "audio_delivered"
            self._log(
                record,
                "audio_delivered",
                role=role,
                voice_id=voice_id,
                voice_name=voice_name,
            )
            self._emit_lifecycle("agent_audio_finished", record)
            if not self.has_pending_delivery_for_current_route():
                self._emit_lifecycle("safe_to_unmute", record)
            return
        if record.status in _TERMINAL_STATUSES:
            return
        record.status = "zero_audio"
        record.terminal_reason = "tts_completed_without_audio"
        record.reserved_for_tts = False
        self._log(
            record,
            "zero_audio",
            role=role,
            voice_id=voice_id,
            voice_name=voice_name,
            reason=record.terminal_reason,
        )
        if not self.has_pending_delivery_for_current_route():
            self._emit_lifecycle(
                "safe_to_unmute", record, reason=record.terminal_reason
            )

    def mark_suppressed_stale(
        self,
        record: VoiceDeliveryRecord,
        *,
        reason: str,
        role: str = "",
        voice_id: str | None = None,
        voice_name: str | None = None,
    ) -> None:
        self._cancel_user_turn_closure_task(record.delivery_id)
        record.status = "suppressed_stale"
        record.terminal_reason = reason
        record.reserved_for_tts = False
        self._log(
            record,
            "suppressed_stale",
            role=role,
            voice_id=voice_id,
            voice_name=voice_name,
            reason=reason,
        )

    def mark_cancelled(
        self,
        record: VoiceDeliveryRecord,
        *,
        reason: str,
        emit_safe_to_unmute: bool = True,
    ) -> None:
        if record.status in _TERMINAL_STATUSES:
            return
        self._cancel_user_turn_closure_task(record.delivery_id)
        record.status = "cancelled"
        record.terminal_reason = reason
        record.reserved_for_tts = False
        self._log(record, "cancelled", reason=reason)
        if emit_safe_to_unmute and not self.has_pending_delivery_for_current_route():
            self._emit_lifecycle("safe_to_unmute", record, reason=reason)

    def has_pending_delivery_for_current_route(self) -> bool:
        current_route = self._route_snapshot()
        return any(
            record.route_at_acceptance.same_route(current_route)
            and record.status
            in {
                "utterance_accepted",
                "answer_owed",
                "text_generated",
                "tts_reserved",
                "tts_flushed",
                "audio_started",
            }
            and not record.delivered
            for record in self._records.values()
        )

    def record_for_turn(self, turn_id: str) -> VoiceDeliveryRecord | None:
        return self._records_by_turn_id.get(turn_id)

    def _cancel_superseded_pending_for_current_route(self) -> None:
        current_route = self._route_snapshot()
        for record in tuple(self._records.values()):
            if (
                record.route_at_acceptance.same_route(current_route)
                and record.status
                in {
                    "utterance_accepted",
                    "answer_owed",
                    "text_generated",
                    "tts_reserved",
                    "tts_flushed",
                }
                and not record.delivered
            ):
                self.mark_cancelled(
                    record,
                    reason="superseded_by_new_utterance",
                    emit_safe_to_unmute=False,
                )

    def _is_stale(self, record: VoiceDeliveryRecord) -> bool:
        route = self._route_snapshot()
        generation_route = record.route_at_text_generation or record.route_at_acceptance
        return not generation_route.same_route(route)

    def _cancel_user_turn_closure_task(self, delivery_id: str) -> None:
        task = self._user_turn_closure_tasks.pop(delivery_id, None)
        if task is not None:
            task.cancel()

    def _log(
        self,
        record: VoiceDeliveryRecord,
        stage: str,
        *,
        role: str = "",
        voice_id: str | None = None,
        voice_name: str | None = None,
        reason: str = "",
    ) -> None:
        route = self._route_snapshot()
        logger.info(
            "dispatch_timing stage=voice_delivery_%s delivery_id=%s message_id=%s "
            "turn_id=%s status=%s prompt_hash=%s prompt_len=%d speech_hash=%s "
            "speech_len=%d tts_text_hash=%s tts_text_len=%d route_version=%d "
            "route_thread_id=%s route=%s route_voice_id=%s role=%s voice_id=%s "
            "voice_name=%s room=%s room_id=%s delivered=%s reason=%s",
            stage,
            record.delivery_id,
            record.message_id,
            record.turn_id or "",
            record.status,
            record.prompt_hash,
            record.prompt_len,
            record.speech_hash,
            record.speech_len,
            record.tts_text_hash,
            record.tts_text_len,
            route.route_version,
            route.active_thread_id,
            route.active_route,
            route.active_voice_id or "",
            role,
            voice_id or "",
            voice_name or "",
            record.room_name,
            record.room_id,
            record.delivered,
            reason,
        )

    def _log_user_turn_closure(
        self,
        record: VoiceDeliveryRecord,
        stage: str,
        *,
        reason: str = "",
    ) -> None:
        route = self._route_snapshot()
        logger.info(
            "dispatch_timing stage=voice_delivery_%s delivery_id=%s message_id=%s "
            "turn_id=%s status=%s route_version=%d route_thread_id=%s route=%s "
            "user_turn_closed=%s user_turn_confidence=%s user_turn_source=%s "
            "user_turn_delay_ms=%s user_turn_completion_reason=%s "
            "user_turn_eou_probability=%s user_turn_silence_ms=%s "
            "user_turn_transcript_confidence=%s user_turn_transcription_delay_ms=%s "
            "prompt_hash=%s "
            "prompt_len=%d room=%s room_id=%s delivered=%s reason=%s",
            stage,
            record.delivery_id,
            record.message_id,
            record.turn_id or "",
            record.status,
            route.route_version,
            route.active_thread_id,
            route.active_route,
            record.user_turn_closed,
            (
                f"{record.user_turn_closure_confidence:.2f}"
                if record.user_turn_closure_confidence is not None
                else ""
            ),
            record.user_turn_closure_source,
            (
                str(record.user_turn_closure_delay_ms)
                if record.user_turn_closure_delay_ms is not None
                else ""
            ),
            record.user_turn_completion_reason,
            (
                f"{record.user_turn_eou_probability:.2f}"
                if record.user_turn_eou_probability is not None
                else ""
            ),
            (
                str(record.user_turn_silence_ms)
                if record.user_turn_silence_ms is not None
                else ""
            ),
            (
                f"{record.user_turn_transcript_confidence:.2f}"
                if record.user_turn_transcript_confidence is not None
                else ""
            ),
            (
                str(record.user_turn_transcription_delay_ms)
                if record.user_turn_transcription_delay_ms is not None
                else ""
            ),
            record.prompt_hash,
            record.prompt_len,
            record.room_name,
            record.room_id,
            record.delivered,
            reason,
        )

    def _apply_user_turn_signals(
        self,
        record: VoiceDeliveryRecord,
        signals: UserTurnClosureSignals | None,
    ) -> None:
        if signals is None:
            return
        record.user_turn_eou_probability = signals.eou_probability
        record.user_turn_silence_ms = signals.silence_ms
        record.user_turn_transcript_confidence = signals.transcript_confidence
        record.user_turn_transcription_delay_ms = (
            max(0, int(signals.transcription_delay * 1000))
            if signals.transcription_delay is not None
            else None
        )

    def _emit_lifecycle(
        self,
        event: str,
        record: VoiceDeliveryRecord,
        *,
        reason: str = "",
    ) -> None:
        if event == "safe_to_mute_user":
            self._mute_covers_current_quiet = True
        elif event == "safe_to_unmute":
            self._mute_covers_current_quiet = False
        if self._lifecycle_sink is None:
            return
        try:
            self._lifecycle_sink(event, record, reason)
        except Exception:
            logger.warning(
                "dispatch_timing stage=voice_delivery_lifecycle_sink_failed "
                "event=%s delivery_id=%s",
                event,
                record.delivery_id,
                exc_info=True,
            )


_TERMINAL_STATUSES = {
    "audio_delivered",
    "zero_audio",
    "cancelled",
    "suppressed_stale",
    "failed",
}


def _tts_match_key(text: str) -> str:
    return re.sub(r"\W+", " ", text.casefold()).strip()


def _tts_texts_compatible(expected: str, actual: str) -> bool:
    expected_key = _tts_match_key(expected)
    actual_key = _tts_match_key(actual)
    if not expected_key or not actual_key:
        return False
    if expected_key == actual_key:
        return True
    shorter, longer = sorted((expected_key, actual_key), key=len)
    if len(shorter) >= 20 and shorter in longer:
        return True
    return difflib.SequenceMatcher(None, expected_key, actual_key).ratio() >= 0.92
