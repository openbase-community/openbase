from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from openbase_coder_cli.services.console_settings import get_user_address_name

STTProviderId = Literal["assemblyai", "openbase_cloud", "deepgram", "local_mlx_whisper"]

ASSEMBLYAI_STT_PROVIDER_ID = "assemblyai"
OPENBASE_CLOUD_STT_PROVIDER_ID = "openbase_cloud"
DEEPGRAM_STT_PROVIDER_ID = "deepgram"
LOCAL_MLX_WHISPER_STT_PROVIDER_ID = "local_mlx_whisper"
DEFAULT_STT_PROVIDER_ID: STTProviderId = ASSEMBLYAI_STT_PROVIDER_ID
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


# MLXWhisperSTT lives in stt_mlx_whisper so importing provider metadata does
# not pull in livekit.agents; re-export lazily for existing importers.
def __getattr__(name: str):
    if name in {"MLXWhisperSTT", "_frame_to_whisper_audio"}:
        from openbase_coder_cli import stt_mlx_whisper

        return getattr(stt_mlx_whisper, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
