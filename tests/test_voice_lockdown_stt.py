from __future__ import annotations

from livekit.agents import stt as livekit_stt

from openbase_coder_cli.livekit_agent.lockdown_stt import VoiceLockdownStream


def _event(kind, text=""):
    alternatives = []
    if text:
        alternatives = [livekit_stt.SpeechData(language="en", text=text, confidence=1.0)]
    return livekit_stt.SpeechEvent(type=kind, alternatives=alternatives)


class FakeStream:
    def __init__(self, events):
        self.events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeBroker:
    def __init__(self):
        self.armed = True
        self.candidates = []

    def is_armed(self, **scope):
        return self.armed

    def verify_final_utterance(self, transcript, **scope):
        self.candidates.append((transcript, scope))
        self.armed = False
        return "authorized"


async def test_armed_partial_and_final_are_consumed_before_outer_wrappers():
    phrase = "amber river cedar lantern velvet harbor"
    broker = FakeBroker()
    outcomes = []
    stream = VoiceLockdownStream(
        FakeStream(
            [
                _event(livekit_stt.SpeechEventType.INTERIM_TRANSCRIPT, "amber river"),
                _event(livekit_stt.SpeechEventType.FINAL_TRANSCRIPT, phrase),
                _event(livekit_stt.SpeechEventType.FINAL_TRANSCRIPT, phrase),
                _event(livekit_stt.SpeechEventType.START_OF_SPEECH),
            ]
        ),
        broker=broker,
        room_sid="room-1",
        participant_identity="owner-1",
        on_outcome=outcomes.append,
    )
    forwarded = await stream.__anext__()
    assert forwarded.type == livekit_stt.SpeechEventType.START_OF_SPEECH
    assert broker.candidates == [
        (phrase, {"room_sid": "room-1", "participant_identity": "owner-1"})
    ]
    assert outcomes == ["authorized"]


async def test_unarmed_transcript_passes_through_unchanged():
    broker = FakeBroker()
    broker.armed = False
    event = _event(livekit_stt.SpeechEventType.FINAL_TRANSCRIPT, "normal request")
    stream = VoiceLockdownStream(
        FakeStream([event]),
        broker=broker,
        room_sid="room-1",
        participant_identity="owner-1",
        on_outcome=lambda _outcome: None,
    )
    assert await stream.__anext__() is event
