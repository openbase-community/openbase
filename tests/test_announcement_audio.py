"""A normal speech-handle return must not conceal failed announcement synthesis."""
from types import SimpleNamespace

import pytest
from livekit import rtc
from livekit.agents import APIError

from openbase_coder_cli.livekit_agent.announcement_audio import (
    AnnouncementSynthesisOutcome, announcement_audio,
)
from openbase_coder_cli.livekit_agent.speech_queue import AnnouncerSpeechQueue
from openbase_coder_cli.livekit_agent.voice_delivery import VoiceDeliveryLedger, VoiceRouteSnapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("failed,empty,zero_frame,interrupted", [
    (False, False, False, False), (True, False, False, False),
    (False, True, False, False), (False, False, True, False),
    (False, False, False, True)])
async def test_provider_outcome_survives_framework_swallowing(failed, empty, zero_frame, interrupted):
    class Stream:
        closed = False
        emitted = False

        def push_text(self, text):
            pass

        def flush(self):
            pass

        def end_input(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            if empty:
                raise StopAsyncIteration
            if self.emitted:
                if failed:
                    raise APIError("simulated provider failure after audio")
                raise StopAsyncIteration
            self.emitted = True
            return SimpleNamespace(frame=rtc.AudioFrame(data=bytes(0 if zero_frame else 320),
                sample_rate=16000, num_channels=1, samples_per_channel=0 if zero_frame else 160))

        async def aclose(self):
            self.closed = True

    stream = Stream()
    tts = SimpleNamespace(stream_for_voice=lambda voice_id: stream)
    outcome = AnnouncementSynthesisOutcome()
    audio = announcement_audio(tts, 'A complete diagnostic sentence.',
        voice_id='background', outcome=outcome)

    class FrameworkHandle:
        async def wait_for_playout(self):
            # Model the real distinction: a framework can finish its handle
            # after logging an audio-generator error instead of rethrowing it.
            try:
                async for _ in audio:
                    pass
            except APIError:
                pass

    ledger = VoiceDeliveryLedger(route_snapshot=lambda: VoiceRouteSnapshot(
        route_version=0, active_thread_id='dispatcher', active_voice_id=None,
        active_voice_name=None, active_route='dispatcher'))
    records = []
    ledger.set_lifecycle_sink(lambda event, record, reason: records.append(record))
    queue = AnnouncerSpeechQueue(session=SimpleNamespace(), announcer_tts=tts,
        delivery_ledger=ledger)
    handle = FrameworkHandle()
    handle.interrupted = interrupted
    await queue._bracketed_playout(handle, text='diagnostic', voice_id='background',
        voice_name=None, synthesis_outcome=outcome)
    assert stream.closed
    record = records[0]
    no_audio = empty or zero_frame
    if interrupted:
        assert outcome.completed
        assert record.status == 'cancelled'
        assert record.terminal_reason == 'announcer_playout_interrupted'
        return
    if failed or no_audio:
        assert record.status == 'failed'
        assert record.terminal_reason == ('tts_provider_failed_after_partial_audio' if failed
            else 'tts_provider_failed_without_audio')
    else:
        assert record.status == 'audio_delivered'
    assert record.audio_events == (0 if no_audio else 1)
    assert record.audio_seconds == (0 if no_audio else .01)
