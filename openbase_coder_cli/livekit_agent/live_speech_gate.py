"""Application speech ownership for a provider that cannot cancel generation."""

import logging
import math
import time
from contextvars import ContextVar

from livekit import rtc
from livekit.agents import Agent

from .config import LIVE_VOICE_BARGE_IN_MIN_SECONDS

logger = logging.getLogger(__name__)
_burst = ContextVar("live_speech_burst", default=None)


class LiveSpeechGate:
    """Discard unsolicited/old audio, including the rest of an interrupted burst.

    GPT-Live's duplex adapter permits overlapping speech and its interrupt is
    a no-op. AgentSession.interrupt clears queued playout; this node prevents
    subsequent provider frames from refilling it. Only application commentary
    or a supplied announcement script authorizes the next answer.

    Caller speech revokes the current burst only once it has lasted
    ``barge_in_min_seconds``: a shorter blip is speakerphone echo or noise,
    and the answer keeps playing through it. An authorization that arrives
    while an unpermitted burst is still streaming is starved (the model folds
    the answer into speech that is being discarded); when that burst closes
    without anything permitted having played, ``on_starved`` is called so
    the owner can ask for the answer again.
    """

    def __init__(
        self,
        *,
        barge_in_min_seconds=LIVE_VOICE_BARGE_IN_MIN_SECONDS,
        clock=time.monotonic,
    ):
        self.epoch = 0
        self.authorized = False
        self.on_starved = None
        self.barge_in_min_seconds = max(0.0, barge_in_min_seconds)
        self._clock = clock
        self._next_burst = 0
        self._open = {}
        self._starved = None
        self._caller_speaking = False
        self._caller_since = 0.0
        self._caller_revoked = False
        self.barge_ins_ignored = 0

    def revoke(self):
        self.epoch += 1
        self.authorized = False
        self._starved = None

    def _revoke_for_caller(self):
        if not self._caller_revoked:
            self._caller_revoked = True
            self.revoke()

    @property
    def speaking(self):
        """The caller is interrupting: speaking for at least the barge-in minimum."""
        if not self._caller_speaking:
            return False
        if self._caller_revoked:
            return True
        if self._clock() - self._caller_since >= self.barge_in_min_seconds:
            self._revoke_for_caller()
            return True
        return False

    def user_state_changed(self, state):
        speaking = state == "speaking"
        if speaking and not self._caller_speaking:
            self._caller_speaking = True
            self._caller_since = self._clock()
            self._caller_revoked = False
            if self.barge_in_min_seconds <= 0:
                self._revoke_for_caller()
        elif not speaking and self._caller_speaking:
            elapsed = self._clock() - self._caller_since
            if not self._caller_revoked:
                if elapsed >= self.barge_in_min_seconds:
                    self._revoke_for_caller()
                else:
                    self.barge_ins_ignored += 1
                    logger.info(
                        "dispatch_timing stage=live_barge_in_ignored duration_ms=%d "
                        "epoch=%d authorized=%s",
                        round(elapsed * 1000),
                        self.epoch,
                        self.authorized,
                    )
            self._caller_speaking = False

    def authorize(self):
        self.authorized = True
        unpermitted = [seq for seq, burst in self._open.items() if not burst.permitted]
        if unpermitted:
            self._starved = max(unpermitted)
            logger.info(
                "dispatch_timing stage=live_authorization_starved burst=%d epoch=%d",
                self._starved,
                self.epoch,
            )

    def _burst_opened(self, burst):
        self._open[burst.sequence] = burst

    def _burst_closed(self, burst):
        self._open.pop(burst.sequence, None)
        if self._starved != burst.sequence or burst.permitted:
            return
        self._starved = None
        if self.authorized and not self.speaking and self.on_starved is not None:
            self.on_starved()

    async def filter_audio(self, audio):
        async for frame in _SpeechBurst(self).filter_audio(audio):
            yield frame


class _SpeechBurst:
    def __init__(self, gate):
        self.gate = gate
        self.epoch = gate.epoch
        self.permitted = gate.authorized and not gate.speaking
        self.reported = False
        gate._next_burst += 1
        self.sequence = gate._next_burst
        gate._burst_opened(self)

    async def filter_audio(self, stream):
        frames = 0
        duration = 0.0
        try:
            async for frame in self.filter(stream):
                if isinstance(frame, rtc.AudioFrame):
                    if frames == 0:
                        samples = frame.data
                        rms = math.sqrt(
                            sum(v * v for v in samples) / max(1, len(samples))
                        )
                        logger.info(
                            "dispatch_timing stage=live_pcm_forwarded burst=%d epoch=%d "
                            "sample_rate=%d channels=%d first_frame_rms_dbfs=%.2f peak=%d",
                            self.sequence,
                            self.epoch,
                            frame.sample_rate,
                            frame.num_channels,
                            20 * math.log10(max(rms / 32768, 1e-6)),
                            max((abs(v) for v in samples), default=0),
                        )
                    frames += 1
                    duration += frame.samples_per_channel / frame.sample_rate
                yield frame
        finally:
            # This proves frames reached the SDK output node, not phone playback.
            # Text-only speech events must not be mistaken for emitted PCM.
            logger.info(
                "dispatch_timing stage=live_pcm_segment_end burst=%d epoch=%d "
                "frames=%d audio_ms=%d permitted=%s",
                self.sequence,
                self.epoch,
                frames,
                round(duration * 1000),
                self.permitted,
            )
            self.gate._burst_closed(self)

    async def filter(self, stream):
        # Never release the tail of a burst that began before authorization,
        # or resume interrupted audio when the caller stops talking.
        async for value in stream:
            self.permitted = (
                self.permitted
                and self.epoch == self.gate.epoch
                and not self.gate.speaking
            )
            if self.permitted:
                yield value
            elif not self.reported:
                logger.info(
                    "dispatch_timing stage=live_audio_discarded epoch=%d "
                    "current_epoch=%d caller_speaking=%s authorized=%s",
                    self.epoch,
                    self.gate.epoch,
                    self.gate.speaking,
                    self.gate.authorized,
                )
                self.reported = True


class SpeechGatedAgent(Agent):
    """Use the public audio/transcript nodes with one permit per generation.

    The SDK invokes both nodes in the same message task. A ContextVar pairs
    them even when multiple generations overlap. Suppressed audio must not
    become a supposedly spoken transcript or seed the next character's history.
    """

    _speech_gate = None

    def realtime_audio_output_node(self, audio, model_settings):
        if self._speech_gate is None:
            return super().realtime_audio_output_node(audio, model_settings)
        permit = _SpeechBurst(self._speech_gate)
        _burst.set(permit)
        return permit.filter_audio(audio)

    def transcription_node(self, text, model_settings):
        permit = _burst.get()
        if self._speech_gate is None or permit is None:
            return super().transcription_node(text, model_settings)
        return permit.filter(text)
