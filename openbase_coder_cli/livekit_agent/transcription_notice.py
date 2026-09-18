"""Explain sustained speech lost before transcription without replaying a task."""
import logging
import time

from .packets import AnnouncerMessage
from .turn_detection import VAD_ONLY_MIN_SPEECH_SECONDS

logger = logging.getLogger(__name__)


class TranscriptionTimeoutNotice:
    def __init__(self, queue, *, clock=time.monotonic):
        self._queue = queue
        self._clock = clock
        self._last_notice_at = float("-inf")

    def timed_out(self, record):
        duration = record.user_speech_seconds
        now = self._clock()
        # Brief VAD triggers can be taps or room noise. Repeated failures get
        # one explanation per minute, not a stream of competing announcements.
        if duration < VAD_ONLY_MIN_SPEECH_SECONDS or now - self._last_notice_at < 60:
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
