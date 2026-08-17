"""LiveKit TTS adapter for the local Kokoro provider.

Kept separate from tts_providers so that importing provider metadata does not
pull in livekit.agents (which drags in the OpenAI SDK and ~0.5s of imports).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from livekit.agents import tts as livekit_tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from openbase_coder_cli.tts_providers import KOKORO_REPO_ID

if TYPE_CHECKING:
    from openbase_coder_cli.tts_providers import KokoroTTSProvider


class KokoroLiveKitTTS(livekit_tts.TTS):
    def __init__(
        self,
        *,
        provider: KokoroTTSProvider,
        voice_id: str,
    ) -> None:
        super().__init__(
            capabilities=livekit_tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )
        self._provider = provider
        self._voice_id = voice_id

    @property
    def model(self) -> str:
        return KOKORO_REPO_ID

    @property
    def provider(self) -> str:
        return "Kokoro"

    def synthesize(
        self,
        text: str,
        *,
        conn_options=DEFAULT_API_CONNECT_OPTIONS,
    ) -> livekit_tts.ChunkedStream:
        return KokoroChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def prewarm(self) -> None:
        self._provider._pipeline_for_voice(self._voice_id)

    def _synthesize_pcm(self, text: str) -> bytes:
        return self._provider.synthesize_pcm(text=text, voice_id=self._voice_id)


class KokoroChunkedStream(livekit_tts.ChunkedStream):
    async def _run(self, output_emitter) -> None:
        output_emitter.initialize(
            request_id=f"kokoro-{uuid.uuid4().hex}",
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
        )
        pcm = await asyncio.to_thread(self._tts._synthesize_pcm, self.input_text)
        if pcm:
            output_emitter.push(pcm)
            output_emitter.flush()
