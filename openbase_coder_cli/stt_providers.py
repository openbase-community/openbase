from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, dataclass
from typing import Literal

import numpy as np
from livekit import rtc
from livekit.agents import stt as livekit_stt
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN

from openbase_coder_cli.services.console_settings import get_user_address_name

STTProviderId = Literal["assemblyai", "openbase_cloud", "deepgram", "local_mlx_whisper"]

ASSEMBLYAI_STT_PROVIDER_ID = "assemblyai"
OPENBASE_CLOUD_STT_PROVIDER_ID = "openbase_cloud"
DEEPGRAM_STT_PROVIDER_ID = "deepgram"
LOCAL_MLX_WHISPER_STT_PROVIDER_ID = "local_mlx_whisper"
DEFAULT_STT_PROVIDER_ID: STTProviderId = OPENBASE_CLOUD_STT_PROVIDER_ID
LOCAL_MLX_WHISPER_MODEL_ID = "mlx-community/whisper-small.en-mlx"


def local_mlx_whisper_prompt() -> str:
    user_address_name = get_user_address_name()
    return (
        "Openbase Coder voice coding vocabulary: "
        f"{user_address_name}, Openbase, Kokoro, Cartesia, Codex, LiveKit, TTS, "
        "STT, Python, React, TypeScript, Swift, Django, pytest, uv, pnpm, GitHub, "
        "pull request."
    )


@dataclass(frozen=True)
class STTDownloadStatus:
    provider: STTProviderId
    ready: bool
    model: str
    detail: str | None = None

    def payload(self) -> dict[str, str | bool | None]:
        return asdict(self)


@dataclass(frozen=True)
class STTProviderOption:
    id: STTProviderId
    name: str
    local: bool
    model: str | None = None

    def payload(self) -> dict[str, str | bool | None]:
        return asdict(self)


STT_PROVIDER_OPTIONS: tuple[STTProviderOption, ...] = (
    STTProviderOption(ASSEMBLYAI_STT_PROVIDER_ID, "AssemblyAI", False),
    STTProviderOption(OPENBASE_CLOUD_STT_PROVIDER_ID, "Openbase Cloud", False),
    STTProviderOption(DEEPGRAM_STT_PROVIDER_ID, "Deepgram", False),
    STTProviderOption(
        LOCAL_MLX_WHISPER_STT_PROVIDER_ID,
        "Local MLX Whisper",
        True,
        LOCAL_MLX_WHISPER_MODEL_ID,
    ),
)


def normalize_stt_provider_id(provider_id: str | None) -> STTProviderId:
    normalized = (provider_id or DEFAULT_STT_PROVIDER_ID).strip().lower()
    if normalized in {"openbase", "openbase-cloud", "cloud"}:
        normalized = OPENBASE_CLOUD_STT_PROVIDER_ID
    if normalized in {"local", "mlx", "mlx_whisper"}:
        normalized = LOCAL_MLX_WHISPER_STT_PROVIDER_ID
    if normalized not in {provider.id for provider in STT_PROVIDER_OPTIONS}:
        raise ValueError(
            "STT provider must be one of: assemblyai, openbase_cloud, deepgram, local_mlx_whisper."
        )
    return normalized  # type: ignore[return-value]


def stt_provider_options_payload() -> list[dict[str, str | bool | None]]:
    return [provider.payload() for provider in STT_PROVIDER_OPTIONS]


def local_mlx_whisper_readiness() -> STTDownloadStatus:
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return STTDownloadStatus(
            provider=LOCAL_MLX_WHISPER_STT_PROVIDER_ID,
            ready=False,
            model=LOCAL_MLX_WHISPER_MODEL_ID,
            detail="MLX Whisper dependencies are not installed.",
        )

    try:
        snapshot_download(LOCAL_MLX_WHISPER_MODEL_ID, local_files_only=True)
    except Exception:
        return STTDownloadStatus(
            provider=LOCAL_MLX_WHISPER_STT_PROVIDER_ID,
            ready=False,
            model=LOCAL_MLX_WHISPER_MODEL_ID,
            detail="Local MLX Whisper model is not downloaded.",
        )

    return STTDownloadStatus(
        provider=LOCAL_MLX_WHISPER_STT_PROVIDER_ID,
        ready=True,
        model=LOCAL_MLX_WHISPER_MODEL_ID,
    )


def download_local_mlx_whisper() -> STTDownloadStatus:
    from huggingface_hub import snapshot_download

    snapshot_download(LOCAL_MLX_WHISPER_MODEL_ID)
    return local_mlx_whisper_readiness()


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
