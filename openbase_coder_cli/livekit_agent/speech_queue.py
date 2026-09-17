"""Announcer speech queue that serializes announcements behind agent speech."""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from collections import deque
from pathlib import Path

import av
from livekit import rtc
from livekit.agents import AgentSession

from openbase_coder_cli.livekit_agent.config import (
    ANNOUNCER_MAX_QUEUE_SIZE,
    ANNOUNCER_SILENCE_GRACE_SECONDS,
    ANNOUNCER_STATE_WAIT_TIMEOUT_SECONDS,
    SUPPORTED_AUDIO_EXTENSIONS,
)
from openbase_coder_cli.livekit_agent.packets import (
    AnnouncerAudioMessage,
    AnnouncerMessage,
    AnnouncerQueueItem,
    QueuedAnnouncerItem,
)
from openbase_coder_cli.livekit_agent.speech_formatter import format_for_speech
from openbase_coder_cli.livekit_agent.tts_selection import VoiceSelectingTTS
from openbase_coder_cli.livekit_agent.announcement_audio import (
    AnnouncementSynthesisOutcome, announcement_audio,
)

logger = logging.getLogger(__name__)


class AnnouncerSpeechQueue:
    """Serializes non-Codex announcer speech behind normal agent speech."""

    def __init__(
        self,
        *,
        session: AgentSession,
        announcer_tts: VoiceSelectingTTS,
        max_queue_size: int = ANNOUNCER_MAX_QUEUE_SIZE,
        silence_grace_seconds: float = ANNOUNCER_SILENCE_GRACE_SECONDS,
        delivery_ledger=None,
    ) -> None:
        self._session = session
        self._announcer_tts = announcer_tts
        self._queue: asyncio.Queue[QueuedAnnouncerItem | None] = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._silence_grace_seconds = max(0.0, silence_grace_seconds)
        self._state_changed = asyncio.Event()
        self._closed = False
        self._worker_task: asyncio.Task[None] | None = None
        self._delivery_ledger = delivery_ledger
        self._speaking = False
        self._recent_message_ids: deque[str] = deque()
        self._recent_message_id_set: set[str] = set()

    def has_pending_announcements(self) -> bool:
        """True while an announcement is queued or playing.

        The delivery ledger holds ``safe_to_unmute`` while this is True so
        the mic does not reopen right as a queued Super Agent intro starts.
        """
        return self._speaking or self._queue.qsize() > 0

    def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(
                self._run(),
                name="openbase-announcer-speech-queue",
            )

    def enqueue(self, message: AnnouncerQueueItem) -> bool:
        if message.message_id and message.message_id in self._recent_message_id_set:
            logger.info("dispatch_timing stage=announcer_duplicate_ignored message_id=%s",
                message.message_id)
            return True
        try:
            self._queue.put_nowait(
                QueuedAnnouncerItem(message=message, enqueued_at=time.monotonic())
            )
        except asyncio.QueueFull:
            logger.warning(
                "dispatch_timing stage=announcer_queue_full message_id=%s "
                "queue_size=%d max_queue_size=%d",
                message.message_id,
                self._queue.qsize(),
                self._queue.maxsize,
            )
            return False
        if message.message_id:
            if len(self._recent_message_ids) >= 256:
                self._recent_message_id_set.remove(self._recent_message_ids.popleft())
            self._recent_message_ids.append(message.message_id)
            self._recent_message_id_set.add(message.message_id)
        text_len = len(message.text) if isinstance(message, AnnouncerMessage) else 0
        logger.info(
            "dispatch_timing stage=announcer_enqueued message_id=%s kind=%s "
            "text_len=%d audio_path=%s voice_id=%s queue_size=%d",
            message.message_id,
            "text" if isinstance(message, AnnouncerMessage) else "audio_file",
            text_len,
            message.audio_path if isinstance(message, AnnouncerAudioMessage) else "",
            message.voice_id if isinstance(message, AnnouncerMessage) else "",
            self._queue.qsize(),
        )
        return True

    def notify_state_changed(self, *_args) -> None:
        self._state_changed.set()

    async def close(self) -> None:
        self._closed = True
        self.notify_state_changed()
        await self._queue.put(None)
        if self._worker_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        await self._announcer_tts.aclose()

    async def _run(self) -> None:
        while True:
            queued_message = await self._queue.get()
            if queued_message is None:
                return
            self._speaking = True
            try:
                await self._speak(
                    queued_message.message,
                    enqueued_at=queued_message.enqueued_at,
                )
            except Exception:
                logger.warning(
                    "Unable to play announcer message %s",
                    queued_message.message.message_id,
                    exc_info=True,
                )
            finally:
                self._speaking = False

    async def _speak(
        self,
        message: AnnouncerQueueItem,
        *,
        enqueued_at: float | None = None,
    ) -> None:
        if isinstance(message, AnnouncerAudioMessage):
            await self._play_audio(message, enqueued_at=enqueued_at)
            return

        started = time.monotonic()
        if enqueued_at is None:
            enqueued_at = started
        logger.info(
            "dispatch_timing stage=announcer_playout_wait_start message_id=%s",
            message.message_id,
        )
        if not await self._wait_until_both_silent(
            message_id=message.message_id,
            enqueued_at=enqueued_at,
        ):
            return

        spoken_text = format_for_speech(message.text)
        if not spoken_text:
            spoken_text = "Technical output omitted, shown on screen."
        logger.info(
            "dispatch_timing stage=announcer_speech_formatted message_id=%s "
            "original_len=%d spoken_len=%d",
            message.message_id,
            len(message.text),
            len(spoken_text),
        )

        if not self._both_silent() and not await self._wait_until_both_silent(
            message_id=message.message_id,
            enqueued_at=enqueued_at,
        ):
            return

        logger.info(
            "dispatch_timing stage=announcer_say_start message_id=%s wait_ms=%d "
            "queue_age_ms=%d voice_id=%s voice_name=%s text_len=%d",
            message.message_id,
            int((time.monotonic() - started) * 1000),
            int((time.monotonic() - enqueued_at) * 1000),
            self._announcer_tts.resolve_voice_id(message.voice_id),
            self._announcer_tts.resolve_voice_name(message.voice_id) or "",
            len(message.text),
        )

        outcome = AnnouncementSynthesisOutcome()
        handle = self._session.say(
            spoken_text,
            audio=announcement_audio(self._announcer_tts, spoken_text,
                voice_id=message.voice_id, outcome=outcome),
            allow_interruptions=False,
            add_to_chat_ctx=False,
        )
        await self._bracketed_playout(
            handle,
            text=spoken_text,
            voice_id=self._announcer_tts.resolve_voice_id(message.voice_id),
            voice_name=self._announcer_tts.resolve_voice_name(message.voice_id),
            synthesis_outcome=outcome,
        )
        logger.info(
            "dispatch_timing stage=announcer_playout_end message_id=%s elapsed_ms=%d "
            "synthesis_completed=%s audio_events=%d",
            message.message_id,
            int((time.monotonic() - started) * 1000),
            outcome.completed, outcome.audio_events,
        )

    async def _bracketed_playout(
        self,
        handle,
        *,
        text: str,
        voice_id: str | None,
        voice_name: str | None,
        synthesis_outcome: AnnouncementSynthesisOutcome | None = None,
    ) -> None:
        """Await playout with voice-lifecycle bracketing around the audio.

        Announcements otherwise play with no lifecycle events at all, so the
        client mic can reopen exactly as the announcement starts.
        """
        ledger = self._delivery_ledger
        if ledger is None:
            await handle.wait_for_playout()
            return
        record = ledger.track_announcement(text=text)
        playout_started = time.monotonic()
        ledger.mark_audio_started(
            record,
            latency_ms=0,
            role="announcer",
            voice_id=voice_id,
            voice_name=voice_name,
        )
        try:
            await handle.wait_for_playout()
        except BaseException:
            ledger.mark_cancelled(record, reason="announcer_playout_failed")
            raise
        # Playout was awaited for real, so completion releases the unmute
        # immediately; clear the speaking flag first so this announcement
        # does not hold its own release.
        self._speaking = False
        if getattr(handle, "interrupted", False):
            logger.warning("dispatch_timing stage=announcer_playout_interrupted "
                "delivery_id=%s synthesis_completed=%s", record.delivery_id,
                synthesis_outcome.completed if synthesis_outcome is not None else None)
            ledger.mark_cancelled(record, reason="announcer_playout_interrupted")
            return
        if synthesis_outcome is not None and (
            not synthesis_outcome.completed or not synthesis_outcome.audio_events
        ):
            logger.warning("dispatch_timing stage=announcer_synthesis_incomplete "
                "delivery_id=%s audio_events=%d audio_seconds=%.2f", record.delivery_id,
                synthesis_outcome.audio_events, synthesis_outcome.audio_seconds)
            ledger.mark_tts_failed(record, audio_events=synthesis_outcome.audio_events,
                audio_seconds=synthesis_outcome.audio_seconds)
            return
        ledger.mark_tts_completed(
            record,
            audio_events=synthesis_outcome.audio_events if synthesis_outcome is not None else 1,
            audio_seconds=synthesis_outcome.audio_seconds if synthesis_outcome is not None
                else time.monotonic() - playout_started,
            role="announcer",
            voice_id=voice_id,
            voice_name=voice_name,
        )

    async def _wait_until_both_silent(
        self,
        *,
        message_id: str,
        enqueued_at: float,
    ) -> bool:
        wait_logged = False
        wait_started = time.monotonic()
        while not self._closed:
            current_speech = self._session.current_speech
            has_current_speech = self._speech_active(current_speech)
            user_state = str(getattr(self._session, "user_state", "") or "")
            if not has_current_speech and user_state != "speaking":
                await self._wait_for_quiet_grace_period()
                current_speech = self._session.current_speech
                has_current_speech = self._speech_active(current_speech)
                user_state = str(getattr(self._session, "user_state", "") or "")
                if not has_current_speech and user_state != "speaking":
                    if wait_logged:
                        logger.info(
                            "dispatch_timing stage=announcer_silence_wait_end "
                            "message_id=%s wait_ms=%d queue_age_ms=%d",
                            message_id,
                            int((time.monotonic() - wait_started) * 1000),
                            int((time.monotonic() - enqueued_at) * 1000),
                        )
                    return True
                continue

            if not wait_logged:
                wait_logged = True
                logger.info(
                    "dispatch_timing stage=announcer_silence_wait_start "
                    "message_id=%s queue_size=%d user_state=%s agent_state=%s "
                    "has_current_speech=%s queue_age_ms=%d",
                    message_id,
                    self._queue.qsize(),
                    user_state,
                    getattr(self._session, "agent_state", "") or "",
                    has_current_speech,
                    int((time.monotonic() - enqueued_at) * 1000),
                )

            if has_current_speech:
                await current_speech.wait_for_playout()
                continue

            await self._wait_for_state_change_or_timeout(
                ANNOUNCER_STATE_WAIT_TIMEOUT_SECONDS
            )

        return False

    def _both_silent(self) -> bool:
        return (
            not self._speech_active(self._session.current_speech)
            and str(getattr(self._session, "user_state", "") or "") != "speaking"
        )

    @staticmethod
    def _speech_active(speech_handle) -> bool:
        return speech_handle is not None and not speech_handle.done()

    async def _wait_for_quiet_grace_period(self) -> None:
        if self._silence_grace_seconds <= 0:
            return
        await self._wait_for_state_change_or_timeout(self._silence_grace_seconds)

    async def _wait_for_state_change_or_timeout(self, timeout_seconds: float) -> None:
        self._state_changed.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._state_changed.wait(), timeout_seconds)

    async def _play_audio(
        self,
        message: AnnouncerAudioMessage,
        *,
        enqueued_at: float | None = None,
    ) -> None:
        started = time.monotonic()
        if enqueued_at is None:
            enqueued_at = started
        audio_path = Path(message.audio_path).expanduser()
        if not audio_path.is_file():
            logger.warning(
                "Unable to play announcer audio %s: file not found",
                message.message_id,
            )
            return
        if audio_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            logger.warning(
                "Unable to play announcer audio %s: unsupported extension %s",
                message.message_id,
                audio_path.suffix.lower(),
            )
            return

        logger.info(
            "dispatch_timing stage=announcer_audio_playout_wait_start message_id=%s",
            message.message_id,
        )
        if not await self._wait_until_both_silent(
            message_id=message.message_id,
            enqueued_at=enqueued_at,
        ):
            return
        if not self._both_silent() and not await self._wait_until_both_silent(
            message_id=message.message_id,
            enqueued_at=enqueued_at,
        ):
            return

        handle = self._session.say(
            "",
            audio=self._audio_file_frames(audio_path),
            allow_interruptions=False,
            add_to_chat_ctx=False,
        )
        await self._bracketed_playout(
            handle,
            text=audio_path.name,
            voice_id=None,
            voice_name=None,
        )
        logger.info(
            "dispatch_timing stage=announcer_audio_playout_end message_id=%s "
            "elapsed_ms=%d audio_basename=%s",
            message.message_id,
            int((time.monotonic() - started) * 1000),
            audio_path.name,
        )

    async def _audio_file_frames(self, path: Path) -> AsyncIterator[rtc.AudioFrame]:
        for frame in _decode_audio_file(path):
            yield frame


def _decode_audio_file(path: Path) -> list[rtc.AudioFrame]:
    frames: list[rtc.AudioFrame] = []
    with av.open(str(path)) as container:
        stream = next((candidate for candidate in container.streams.audio), None)
        if stream is None:
            raise ValueError(f"No audio stream found in {path.name}.")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        for packet in container.demux(stream):
            for decoded in packet.decode():
                for resampled in resampler.resample(decoded):
                    frames.append(_av_frame_to_livekit_frame(resampled))
        for resampled in resampler.resample(None):
            frames.append(_av_frame_to_livekit_frame(resampled))
    return frames


def _av_frame_to_livekit_frame(frame) -> rtc.AudioFrame:
    data = bytes(frame.planes[0])
    return rtc.AudioFrame(
        data=data,
        sample_rate=frame.sample_rate,
        num_channels=len(frame.layout.channels),
        samples_per_channel=frame.samples,
    )
