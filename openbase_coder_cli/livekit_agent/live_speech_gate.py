"""Application speech ownership for a provider that cannot cancel generation."""

import logging
import math
import re
import time
from collections import deque
from contextvars import ContextVar

from livekit import rtc
from livekit.agents import Agent

from .config import LIVE_VOICE_BARGE_IN_MIN_SECONDS

logger = logging.getLogger(__name__)
_burst = ContextVar("live_speech_burst", default=None)

# After a permitted burst ends, its sound is still playing on the phone and
# echoing back for this long (playout buffer plus the network round trip).
ECHO_TAIL_SECONDS = 1.5
# What the agent said in this window is what its echo can transcribe to.
AGENT_WORDS_WINDOW_SECONDS = 20.0
# A caller fragment heard over the agent is echo when at least this share of
# its words are words the agent just said.
ECHO_WORD_SHARE = 0.5
_WORD = re.compile(r"[a-z0-9']+")


def _words(text):
    return _WORD.findall((text or "").lower())


class LiveSpeechGate:
    """Discard unsolicited/old audio, including the rest of an interrupted burst.

    GPT-Live's duplex adapter permits overlapping speech and its interrupt is
    a no-op. AgentSession.interrupt clears queued playout; this node prevents
    subsequent provider frames from refilling it. Only application commentary
    or a supplied announcement script authorizes the next answer.

    Caller speech revokes the current burst only once it has lasted
    ``barge_in_min_seconds``: a shorter blip is speakerphone echo or noise,
    and the answer keeps playing through it. While the agent is audible
    (a permitted burst is streaming, or ended under ``ECHO_TAIL_SECONDS``
    ago), voice activity alone never revokes: on a speakerphone it is mostly
    the agent's own voice coming back, and a 0.5 s rule cut every answer
    after about a second (Maritime 376, 2026-10-10; reproduced headless by
    ``voice-tests/gptlive_cutoff_probe.py --vad --echo-db -30``). There the
    caller interrupts only once GPT-Live transcribes words from them that the
    agent did not just say (``caller_heard``). An authorization that arrives
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
        self._pending_limit_ms = None
        # Called once per caller barge-in, when it revokes: the owner clears
        # queued playout (AgentSession.interrupt).
        self.on_barge_in = None
        self._agent_words = deque()
        self._last_permitted_end = None
        self._agent_state_speaking = False
        self._caller_over_agent = False
        self._caller_heard = ""

    def revoke(self):
        self.epoch += 1
        self.authorized = False
        self._starved = None
        self._pending_limit_ms = None

    def _revoke_for_caller(self, source="vad"):
        if not self._caller_revoked:
            self._caller_revoked = True
            logger.info(
                "dispatch_timing stage=live_barge_in source=%s vad_ms=%d "
                "agent_audible=%s epoch=%d heard=%r",
                source,
                round((self._clock() - self._caller_since) * 1000),
                self._caller_over_agent,
                self.epoch,
                self._caller_heard[-80:],
            )
            self.revoke()
            if self.on_barge_in is not None:
                self.on_barge_in()

    def agent_state_changed(self, state):
        """The session's agent state: "speaking" while any agent audio plays.

        Covers what the session plays outside the model's bursts
        (``session.say``: route announcer, announcer audio files), whose
        speakerphone echo otherwise counted as the caller; the greeting after
        the route announcer then opened muted (VM2, 2026-10-11 01:18Z).
        """
        speaking = state == "speaking"
        if self._agent_state_speaking and not speaking:
            self._last_permitted_end = self._clock()
        self._agent_state_speaking = speaking

    @property
    def agent_audible(self):
        """The agent's voice is playing, or its sound is still echoing back."""
        if self._agent_state_speaking:
            return True
        if any(burst.permitted for burst in self._open.values()):
            return True
        end = self._last_permitted_end
        return end is not None and self._clock() - end < ECHO_TAIL_SECONDS

    def agent_said(self, text):
        """Words of the agent's output transcript: what its echo sounds like."""
        now = self._clock()
        for word in _words(text):
            self._agent_words.append((now, word))
        while self._agent_words and now - self._agent_words[0][0] > (
            AGENT_WORDS_WINDOW_SECONDS
        ):
            self._agent_words.popleft()

    def is_echo(self, text):
        """True when most of ``text``'s words are words the agent just said."""
        words = _words(text)
        if not words:
            return True
        said = {word for _, word in self._agent_words}
        return sum(word in said for word in words) / len(words) >= ECHO_WORD_SHARE

    def caller_heard(self, text):
        """A caller transcript fragment from the model (input transcript delta).

        Over the agent's own speech this is the only evidence of a real
        interruption that echo cannot fake.
        """
        if not self._caller_speaking or self._caller_revoked or not text:
            return
        self._caller_heard += text
        if self._caller_over_agent and not self.is_echo(self._caller_heard):
            self._revoke_for_caller(source="transcript")

    @property
    def speaking(self):
        """The caller is interrupting: speaking for at least the barge-in minimum.

        Over the agent's own speech, only once a transcript proves it.
        """
        if not self._caller_speaking:
            return False
        if self._caller_revoked:
            return True
        if self._caller_over_agent:
            return False
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
            self._caller_heard = ""
            self._caller_over_agent = self.agent_audible
            if self.barge_in_min_seconds <= 0 and not self._caller_over_agent:
                self._revoke_for_caller()
        elif not speaking and self._caller_speaking:
            elapsed = self._clock() - self._caller_since
            if not self._caller_revoked and self._caller_over_agent:
                self.barge_ins_ignored += 1
                logger.info(
                    "dispatch_timing stage=live_barge_in_echo_ignored vad_ms=%d "
                    "epoch=%d heard=%r",
                    round(elapsed * 1000),
                    self.epoch,
                    self._caller_heard[-80:],
                )
            elif not self._caller_revoked:
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

    def authorize(self, *, limit_ms=None):
        """Permit the next burst; with ``limit_ms`` only that much of it.

        A bounded permit covers one burst (an acknowledgment line) and is
        revoked when that burst ends or the cap is reached, so the model's
        own continuation after it never plays.
        """
        self.authorized = True
        self._pending_limit_ms = limit_ms
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
        if burst.permitted:
            self._last_permitted_end = self._clock()
        if burst.limit_ms is not None and burst.permitted:
            # The bounded permit is spent with its burst.
            if self._pending_limit_ms is None and self.authorized:
                self.revoke()
            return
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
        self.limit_ms = gate._pending_limit_ms if self.permitted else None
        if self.permitted:
            gate._pending_limit_ms = None
        self.forwarded_ms = 0.0
        gate._next_burst += 1
        self.sequence = gate._next_burst
        gate._burst_opened(self)

    async def filter_audio(self, stream):
        frames = 0
        duration = 0.0
        try:
            async for frame in self.filter(stream):
                if isinstance(frame, rtc.AudioFrame):
                    if self.limit_ms is not None and self.forwarded_ms >= self.limit_ms:
                        # The acknowledgment is over; whatever follows in this
                        # burst is the model talking on its own.
                        logger.info(
                            "dispatch_timing stage=live_ack_limit_reached burst=%d "
                            "limit_ms=%d",
                            self.sequence,
                            self.limit_ms,
                        )
                        self.gate.revoke()
                        self.permitted = False
                        continue
                    self.forwarded_ms += (
                        frame.samples_per_channel / frame.sample_rate * 1000
                    )
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
