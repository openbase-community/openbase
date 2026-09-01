"""Local MLX Whisper STT for LiveKit.

Kept separate from stt_providers so that importing provider metadata does not
pull in livekit.agents (which drags in the OpenAI SDK and ~0.5s of imports).
"""

from __future__ import annotations

import asyncio
import uuid

import numpy as np
from livekit import rtc
from livekit.agents import stt as livekit_stt
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN

from openbase_coder_cli.stt_providers import (
    LOCAL_MLX_WHISPER_MODEL_ID,
    local_mlx_whisper_prompt,
)


class MLXWhisperSTT(livekit_stt.STT):
    def __init__(
        self,
        *,
        model: str = LOCAL_MLX_WHISPER_MODEL_ID,
        initial_prompt: str | None = None,
    ) -> None:
        super().__init__(
            capabilities=livekit_stt.STTCapabilities(
                streaming=False,
                interim_results=False,
            )
        )
        self._model = model
        self._initial_prompt = initial_prompt or local_mlx_whisper_prompt()

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "MLX Whisper"

    async def _recognize_impl(
        self,
        buffer,
        *,
        language=NOT_GIVEN,
        conn_options=DEFAULT_API_CONNECT_OPTIONS,
    ) -> livekit_stt.SpeechEvent:
        text = await asyncio.to_thread(self._transcribe_buffer, buffer)
        return livekit_stt.SpeechEvent(
            type=livekit_stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=f"mlx-whisper-{uuid.uuid4().hex}",
            alternatives=[
                livekit_stt.SpeechData(
                    language="en",
                    text=text,
                    confidence=1.0 if text else 0.0,
                )
            ],
        )

    def prewarm(self) -> None:
        import mlx_whisper

        mlx_whisper.load_models.load_model(self._model)

    def _transcribe_buffer(self, buffer) -> str:
        import mlx_whisper

        frame = rtc.combine_audio_frames(buffer)
        audio = _frame_to_whisper_audio(frame)
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self._model,
            language="en",
            task="transcribe",
            initial_prompt=self._initial_prompt,
            verbose=False,
        )
        return " ".join(str(result.get("text") or "").strip().split())


def _frame_to_whisper_audio(frame: rtc.AudioFrame) -> np.ndarray:
    if frame.sample_rate != 16000:
        resampler = rtc.AudioResampler(
            input_rate=frame.sample_rate,
            output_rate=16000,
            num_channels=frame.num_channels,
        )
        frames = resampler.push(frame) + resampler.flush()
        frame = rtc.combine_audio_frames(frames)

    pcm = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32)
    if frame.num_channels > 1:
        pcm = pcm.reshape(-1, frame.num_channels).mean(axis=1)
    return pcm / 32768.0
