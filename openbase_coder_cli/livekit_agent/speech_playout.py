"""Connect a synthesis record to the SDK's actual interruption outcome."""

from livekit.agents.voice.agent_activity import _SpeechHandleContextVar


def bind_interruption(ledger, record):
    # LiveKit propagates this context into each speech task and its TTS tasks.
    # Keep the private SDK dependency here; never guess from current_speech,
    # which may already refer to the next overlapping response.
    handle = _SpeechHandleContextVar.get(None)
    if handle is None:
        return

    def completed(speech):
        if speech.interrupted:
            ledger.mark_playout_interrupted(record)

    handle.add_done_callback(completed)
