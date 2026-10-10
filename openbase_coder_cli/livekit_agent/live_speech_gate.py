"""Application speech ownership for a provider that cannot cancel generation."""

import logging
from contextvars import ContextVar

from livekit.agents import Agent

logger = logging.getLogger(__name__)
_burst = ContextVar("live_speech_burst", default=None)


class LiveSpeechGate:
    """Discard unsolicited/old audio, including the rest of an interrupted burst.

    GPT-Live's duplex adapter permits overlapping speech and its interrupt is
    a no-op. AgentSession.interrupt clears queued playout; this node prevents
    subsequent provider frames from refilling it. Only application commentary
    or a supplied announcement script authorizes the next answer.
    """

    def __init__(self):
        self.epoch = 0
        self.speaking = False
        self.authorized = False

    def revoke(self):
        self.epoch += 1
        self.authorized = False

    def user_state_changed(self, state):
        speaking = state == "speaking"
        if speaking and not self.speaking:
            self.revoke()
        self.speaking = speaking

    def authorize(self):
        self.authorized = True

    async def filter_audio(self, audio):
        async for frame in _SpeechBurst(self).filter(audio):
            yield frame


class _SpeechBurst:
    def __init__(self, gate):
        self.gate = gate
        self.epoch = gate.epoch
        self.permitted = gate.authorized and not gate.speaking
        self.reported = False

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
        return permit.filter(audio)

    def transcription_node(self, text, model_settings):
        permit = _burst.get()
        if self._speech_gate is None or permit is None:
            return super().transcription_node(text, model_settings)
        return permit.filter(text)
