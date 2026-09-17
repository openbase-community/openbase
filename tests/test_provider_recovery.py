"""Exercise the SDK retry boundary that previously terminated a live voice session."""
import asyncio
from dataclasses import replace

import pytest
from livekit.agents import stt, APIConnectionError
from livekit.agents.types import APIConnectOptions
from openbase_coder_cli.livekit_agent.provider_recovery import voice_connect_options


@pytest.mark.asyncio
@pytest.mark.parametrize('extended', [False, True])
async def test_recognition_recovers_after_old_retry_budget_is_exhausted(extended):
    class Provider(stt.STT):
        def __init__(self):
            super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=True))
            self.attempts = 0

        async def _recognize_impl(self, buffer, *, language, conn_options):
            raise NotImplementedError

    class Stream(stt.SpeechStream):
        async def _run(self):
            provider.attempts += 1
            if provider.attempts <= 4:
                raise APIConnectionError('simulated recognition connection loss')
            self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(language='en', text='recognition recovered')]))

    provider = Provider()
    errors = []
    provider.on('error', lambda event: errors.append(event))
    options = voice_connect_options().stt_conn_options if extended else APIConnectOptions()
    # Keep the real retry count; accelerate only the test's backoff clock.
    options = replace(options, retry_interval=.001)
    stream = Stream(stt=provider, conn_options=options)
    try:
        if extended:
            results = [event async for event in stream]
            assert results[-1].alternatives[0].text == 'recognition recovered'
            assert provider.attempts == 5
            assert errors and all(event.recoverable for event in errors)
        else:
            with pytest.raises(APIConnectionError):
                async for _ in stream:
                    pass
            assert errors[-1].recoverable is False
    finally:
        await stream.aclose()
