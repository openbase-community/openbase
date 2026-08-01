from __future__ import annotations

from types import SimpleNamespace

from openbase_coder_cli import local_audio_readiness as readiness_module


def test_local_audio_readiness_reports_missing_runtime_package(monkeypatch) -> None:
    monkeypatch.setattr(
        readiness_module.importlib.util,
        "find_spec",
        lambda module: None if module == "kokoro" else SimpleNamespace(),
    )

    readiness = readiness_module.local_audio_readiness(
        tts_provider_id="kokoro",
        stt_provider_id="local_mlx_whisper",
    )

    assert readiness.ready is False
    assert "kokoro" in readiness.detail


def test_local_audio_readiness_checks_both_model_caches(monkeypatch) -> None:
    monkeypatch.setattr(
        readiness_module.importlib.util,
        "find_spec",
        lambda _module: SimpleNamespace(),
    )
    monkeypatch.setattr(
        readiness_module,
        "get_tts_provider",
        lambda _provider: SimpleNamespace(
            readiness=lambda: SimpleNamespace(
                ready=True,
                detail=None,
                cached_files=30,
                required_files=30,
            )
        ),
    )
    monkeypatch.setattr(
        readiness_module,
        "local_mlx_whisper_readiness",
        lambda: SimpleNamespace(ready=True, detail=None),
    )

    readiness = readiness_module.local_audio_readiness(
        tts_provider_id="kokoro",
        stt_provider_id="local_mlx_whisper",
    )

    assert readiness.ready is True


def test_local_audio_readiness_reports_kokoro_cache_count(monkeypatch) -> None:
    monkeypatch.setattr(
        readiness_module.importlib.util,
        "find_spec",
        lambda _module: SimpleNamespace(),
    )
    monkeypatch.setattr(
        readiness_module,
        "get_tts_provider",
        lambda _provider: SimpleNamespace(
            readiness=lambda: SimpleNamespace(
                ready=False,
                detail="Kokoro model or voice files are missing.",
                cached_files=0,
                required_files=30,
            )
        ),
    )

    readiness = readiness_module.local_audio_readiness(
        tts_provider_id="kokoro",
        stt_provider_id="local_mlx_whisper",
    )

    assert readiness.ready is False
    assert "0/30 cached" in readiness.detail
