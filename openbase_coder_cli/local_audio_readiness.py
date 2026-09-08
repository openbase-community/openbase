"""Readiness checks for the optional local voice pipeline."""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from openbase_coder_cli.stt_providers import (
    LOCAL_MLX_WHISPER_STT_PROVIDER_ID,
    local_mlx_whisper_readiness,
)
from openbase_coder_cli.tts_providers import KOKORO_PROVIDER_ID, get_tts_provider


@dataclass(frozen=True)
class LocalAudioReadiness:
    ready: bool
    detail: str | None = None


def local_audio_readiness(
    *,
    tts_provider_id: str,
    stt_provider_id: str,
) -> LocalAudioReadiness:
    """Check imports and model caches for selected local providers."""
    modules = []
    if tts_provider_id == KOKORO_PROVIDER_ID:
        modules.append("kokoro")
    if stt_provider_id == LOCAL_MLX_WHISPER_STT_PROVIDER_ID:
        modules.append("mlx_whisper")

    missing_modules = []
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception:  # A broken native dependency is also not ready.
            missing_modules.append(module)
    if missing_modules:
        return LocalAudioReadiness(
            ready=False,
            detail="Missing or unusable local audio packages: "
            + ", ".join(missing_modules),
        )

    if tts_provider_id == KOKORO_PROVIDER_ID:
        status = get_tts_provider(KOKORO_PROVIDER_ID).readiness()
        if not status.ready:
            return LocalAudioReadiness(
                ready=False,
                detail=status.detail or "Kokoro model or voice files are missing",
            )

    if stt_provider_id == LOCAL_MLX_WHISPER_STT_PROVIDER_ID:
        status = local_mlx_whisper_readiness()
        if not status.ready:
            return LocalAudioReadiness(
                ready=False,
                detail=status.detail or "Local MLX Whisper model is missing",
            )

    return LocalAudioReadiness(ready=True)
