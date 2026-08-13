"""Shared constraints for the local (Kokoro TTS + MLX Whisper STT) audio stack."""

from __future__ import annotations

import sys

# Kokoro declares python <3.13, so local audio cannot run on newer runtimes.
LOCAL_AUDIO_PYTHON_MAX = (3, 13)


def local_audio_python_error() -> str | None:
    """Explain why local audio cannot run on this Python, or None if it can."""
    if sys.version_info[:2] < LOCAL_AUDIO_PYTHON_MAX:
        return None
    return (
        "Local audio requires a Python 3.12 Openbase Coder runtime because "
        "Kokoro declares Python <3.13; this runtime is Python "
        f"{sys.version_info.major}.{sys.version_info.minor}. Use the Openbase "
        "Cloud or Cartesia audio provider instead."
    )
