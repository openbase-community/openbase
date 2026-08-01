"""One readiness gate for the optional local voice pipeline."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass

from openbase_coder_cli.stt_providers import (
    LOCAL_MLX_WHISPER_STT_PROVIDER_ID,
    local_mlx_whisper_readiness,
)
from openbase_coder_cli.tts_providers import KOKORO_PROVIDER_ID, get_tts_provider

LOCAL_AUDIO_RUNTIME_MODULES = (
    "huggingface_hub",
    "kokoro",
    "mlx_whisper",
    "en_core_web_sm",
)


@dataclass(frozen=True)
class LocalAudioReadiness:
    ready: bool
    detail: str | None = None


def local_audio_readiness(
    *, tts_provider_id: str, stt_provider_id: str
) -> LocalAudioReadiness:
    """Check dependencies and cached models used by the selected providers."""
    uses_local_audio = (
        tts_provider_id == KOKORO_PROVIDER_ID
        or stt_provider_id == LOCAL_MLX_WHISPER_STT_PROVIDER_ID
    )
    if not uses_local_audio:
        return LocalAudioReadiness(ready=True)

    missing_modules = [
        module
        for module in LOCAL_AUDIO_RUNTIME_MODULES
        if importlib.util.find_spec(module) is None
    ]
    if missing_modules:
        return LocalAudioReadiness(
            ready=False,
            detail="Missing local audio Python packages: " + ", ".join(missing_modules),
        )

    if tts_provider_id == KOKORO_PROVIDER_ID:
        tts_status = get_tts_provider(KOKORO_PROVIDER_ID).readiness()
        if not tts_status.ready:
            detail = tts_status.detail or "Kokoro model or voice files are missing."
            return LocalAudioReadiness(
                ready=False,
                detail=(
                    f"{detail.rstrip('.')} "
                    f"({tts_status.cached_files}/{tts_status.required_files} cached)."
                ),
            )

    if stt_provider_id == LOCAL_MLX_WHISPER_STT_PROVIDER_ID:
        stt_status = local_mlx_whisper_readiness()
        if not stt_status.ready:
            return LocalAudioReadiness(
                ready=False,
                detail=stt_status.detail or "Local MLX Whisper model is missing.",
            )

    return LocalAudioReadiness(ready=True)
