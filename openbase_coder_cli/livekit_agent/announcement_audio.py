"""Track synthesis independently of a speech handle's playout completion."""
from dataclasses import dataclass


@dataclass
class AnnouncementSynthesisOutcome:
    completed: bool = False
    audio_events: int = 0
    audio_seconds: float = 0.0


async def announcement_audio(tts, text, *, voice_id, outcome):
    stream = tts.stream_for_voice(voice_id)
    try:
        stream.push_text(text)
        stream.flush()
        stream.end_input()
        async for event in stream:
            frame = getattr(event, "frame", None)
            if frame is None:
                continue
            outcome.audio_events += 1
            if frame.sample_rate:
                outcome.audio_seconds += frame.samples_per_channel / frame.sample_rate
            yield frame
    finally:
        await stream.aclose()
    outcome.completed = True
