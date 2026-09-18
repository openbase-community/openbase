"""Explain sustained speech lost before transcription without replaying a task."""
import logging
import time

from .packets import AnnouncerMessage

logger = logging.getLogger(__name__)


class TranscriptionTimeoutNotice:
    def __init__(self, queue, *, clock=time.monotonic):
        self._queue = queue
        self._clock = clock
        self._speech_started_at = None
        self._speech_seconds = 0.0
        self._last_notice_at = float("-inf")

    def user_state_changed(self, *, new_state, old_state=""):
        now = self._clock()
        if new_state == "speaking":
            if self._speech_started_at is None:
                self._speech_started_at = now
        elif old_state == "speaking" and self._speech_started_at is not None:
            self._speech_seconds += max(0.0, now - self._speech_started_at)
            self._speech_started_at = None

    def final_transcript(self):
        self._speech_seconds = 0.0
        if self._speech_started_at is not None:
            self._speech_started_at = self._clock()

    def timed_out(self, record):
        duration = self._speech_seconds
        self._speech_seconds = 0.0
        now = self._clock()
        # Brief VAD triggers can be taps or room noise. Repeated failures get
        # one explanation per minute, not a stream of competing announcements.
        if duration < 0.75 or now - self._last_notice_at < 60:
            return
        message = AnnouncerMessage(
            message_id=f"transcription-recovery-{record.delivery_id}",
            text="Speech recognition is having trouble. Please check your last request before repeating it.",
            voice_id=record.route_at_acceptance.active_voice_id,
        )
        # The ordinary queue waits for the user's quiet floor and brackets
        # actual playback with the same native mute/unmute lifecycle.
        if self._queue.enqueue(message):
            self._last_notice_at = now
            logger.info("dispatch_timing stage=transcription_timeout_notice_enqueued "
                        "delivery_id=%s speech_ms=%d", record.delivery_id, int(duration * 1000))
