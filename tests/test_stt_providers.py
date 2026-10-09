from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
from livekit import rtc

from openbase_coder_cli.stt_providers import (
    OPENBASE_CLOUD_STT_ENCODING,
    OPENBASE_CLOUD_STT_MODEL,
    OPENBASE_CLOUD_STT_SAMPLE_RATE,
    MLXWhisperSTT,
    _frame_to_whisper_audio,
    local_mlx_whisper_prompt,
)


def _audio_frame(
    *,
    sample_rate: int = 48000,
    num_channels: int = 1,
    samples_per_channel: int = 480,
    value: int = 1000,
) -> rtc.AudioFrame:
    frame = rtc.AudioFrame.create(sample_rate, num_channels, samples_per_channel)
    np.frombuffer(frame.data, dtype=np.int16)[:] = value
    return frame


def test_frame_to_whisper_audio_resamples_to_16khz_float_mono() -> None:
    frame = _audio_frame(sample_rate=48000, num_channels=1, samples_per_channel=480)

    audio = _frame_to_whisper_audio(frame)

    assert audio.dtype == np.float32
    assert audio.shape == (160,)
    assert np.max(np.abs(audio)) <= 1.0


def test_frame_to_whisper_audio_downmixes_stereo() -> None:
    frame = _audio_frame(sample_rate=16000, num_channels=2, samples_per_channel=160)
    pcm = np.frombuffer(frame.data, dtype=np.int16).reshape(-1, 2)
    pcm[:, 0] = 1000
    pcm[:, 1] = 3000

    audio = _frame_to_whisper_audio(frame)

    assert audio.shape == (160,)
    assert np.allclose(audio, np.float32(2000 / 32768.0))


def test_mlx_whisper_stt_passes_audio_array_without_ffmpeg(monkeypatch) -> None:
    transcribe_calls = []

    def fake_transcribe(audio, **kwargs):
        transcribe_calls.append((audio, kwargs))
        return {"text": " hello openbase "}

    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        SimpleNamespace(transcribe=fake_transcribe),
    )
    stt = MLXWhisperSTT()

    text = stt._transcribe_buffer([_audio_frame()])

    assert text == "hello openbase"
    audio, kwargs = transcribe_calls[0]
    assert isinstance(audio, np.ndarray)
    assert kwargs["language"] == "en"
    assert kwargs["task"] == "transcribe"


def test_local_mlx_whisper_prompt_uses_configured_user_address(monkeypatch) -> None:
    monkeypatch.setattr(
        "openbase_coder_cli.stt_providers.get_user_address_name",
        lambda: "Sam",
    )

    prompt = local_mlx_whisper_prompt()

    assert "Sam" in prompt
    assert "Gabe" not in prompt


def test_openbase_cloud_stt_contract_matches_the_assemblyai_plugin_defaults() -> None:
    """The phones' composer dictation mirrors these values (dev-docs/dictation.md);
    a plugin upgrade that moves its defaults must be a deliberate contract change."""
    import inspect

    from livekit.plugins import assemblyai

    defaults = {
        name: parameter.default
        for name, parameter in inspect.signature(assemblyai.STT.__init__).parameters.items()
    }

    assert defaults["model"] == OPENBASE_CLOUD_STT_MODEL
    assert defaults["sample_rate"] == OPENBASE_CLOUD_STT_SAMPLE_RATE
    assert defaults["encoding"] == OPENBASE_CLOUD_STT_ENCODING
