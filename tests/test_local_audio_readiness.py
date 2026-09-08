from __future__ import annotations

from types import SimpleNamespace

from openbase_coder_cli import local_audio_readiness as readiness_module


def test_local_audio_readiness_reports_unusable_runtime_package(monkeypatch) -> None:
    def fake_import(module: str):
        if module == "kokoro":
            raise ImportError("broken package")
        return SimpleNamespace()

    monkeypatch.setattr(readiness_module.importlib, "import_module", fake_import)

    readiness = readiness_module.local_audio_readiness(
        tts_provider_id="kokoro",
        stt_provider_id="local_mlx_whisper",
    )

    assert readiness.ready is False
    assert "kokoro" in readiness.detail


def test_local_audio_readiness_checks_selected_model_caches(monkeypatch) -> None:
    monkeypatch.setattr(
        readiness_module.importlib,
        "import_module",
        lambda _module: SimpleNamespace(),
    )
    monkeypatch.setattr(
        readiness_module,
        "get_tts_provider",
        lambda _provider: SimpleNamespace(
            readiness=lambda: SimpleNamespace(ready=True, detail=None)
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
