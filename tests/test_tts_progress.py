import asyncio

import pytest

from openbase_coder_cli.livekit_agent.tts_progress import TTSProgressGuard, TTSStreamStalled


class ControlledStream:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.cancelled = False

    async def __anext__(self):
        try:
            return await self.queue.get()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_waiting_iterator_arms_when_text_is_submitted_later():
    async def run():
        stream = ControlledStream()
        guard = TTSProgressGuard(first_audio_seconds=.03)
        task = asyncio.create_task(guard.next_event(stream))
        await asyncio.sleep(.05)
        assert not task.done()  # An idle, unsubmitted stream has no deadline.
        guard.submitted()
        with pytest.raises(TTSStreamStalled) as failure:
            await asyncio.wait_for(task, .3)
        assert not failure.value.retryable
        assert stream.cancelled
    asyncio.run(run())


def test_non_audio_events_do_not_extend_silent_stream_deadline():
    async def run():
        stream = ControlledStream()
        guard = TTSProgressGuard(first_audio_seconds=.04)
        guard.submitted()
        for _ in range(3):
            stream.queue.put_nowait("metadata")
            assert await guard.next_event(stream) == "metadata"
            await asyncio.sleep(.01)
        with pytest.raises(TTSStreamStalled):
            await asyncio.wait_for(guard.next_event(stream), .2)
    asyncio.run(run())


def test_delayed_audio_recovers_and_advances_deadline():
    async def run():
        stream = ControlledStream()
        guard = TTSProgressGuard(first_audio_seconds=.12, audio_gap_seconds=.12)
        guard.submitted()
        for _ in range(3):
            pending = asyncio.create_task(guard.next_event(stream))
            await asyncio.sleep(.04)
            stream.queue.put_nowait("audio")
            assert await pending == "audio"
            guard.audio_received()
        with pytest.raises(TTSStreamStalled):
            await asyncio.wait_for(guard.next_event(stream), .3)
    asyncio.run(run())


def test_cancelling_consumer_does_not_leave_pending_provider_read():
    async def run():
        stream = ControlledStream()
        task = asyncio.create_task(TTSProgressGuard().next_event(stream))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.cancelled
    asyncio.run(run())
