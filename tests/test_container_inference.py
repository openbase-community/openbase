from unittest.mock import Mock

from livekit.agents.inference_runner import _InferenceRunner
from livekit.plugins.turn_detector.english import _EUORunnerEn
from livekit.plugins.turn_detector.multilingual import _EUORunnerMultilingual

from openbase_coder_cli.livekit_agent import lazy_inference


def test_desktop_keeps_registered_runners(monkeypatch):
    runners = {
        _EUORunnerEn.INFERENCE_METHOD: _EUORunnerEn,
        _EUORunnerMultilingual.INFERENCE_METHOD: _EUORunnerMultilingual,
    }
    monkeypatch.setattr(_InferenceRunner, "registered_runners", runners.copy())
    monkeypatch.delenv("OPENBASE_CODER_SERVICE_SUPERVISOR", raising=False)
    lazy_inference.configure_container_inference()
    assert _InferenceRunner.registered_runners == runners


def test_container_does_not_initialize_unused_english_model(monkeypatch):
    monkeypatch.setattr(
        _InferenceRunner,
        "registered_runners",
        {
            _EUORunnerEn.INFERENCE_METHOD: _EUORunnerEn,
            _EUORunnerMultilingual.INFERENCE_METHOD: _EUORunnerMultilingual,
        },
    )
    monkeypatch.setenv("OPENBASE_CODER_SERVICE_SUPERVISOR", "external")
    lazy_inference.configure_container_inference()
    assert list(_InferenceRunner.registered_runners.values()) == [
        lazy_inference.LazyMultilingualRunner
    ]


def test_weights_load_once_on_first_inference(monkeypatch):
    initialize = Mock()
    run = Mock(return_value=b"prediction")
    monkeypatch.setattr(_EUORunnerMultilingual, "initialize", initialize)
    monkeypatch.setattr(_EUORunnerMultilingual, "run", run)
    runner = lazy_inference.LazyMultilingualRunner()
    runner.initialize()
    initialize.assert_not_called()
    assert runner.run(b"one") == b"prediction"
    assert runner.run(b"two") == b"prediction"
    initialize.assert_called_once()
    assert run.call_count == 2


def test_container_caps_stale_pool_override(monkeypatch):
    from openbase_coder_cli.livekit_agent import livekit

    monkeypatch.setenv("OPENBASE_CODER_SERVICE_SUPERVISOR", "external")
    monkeypatch.setenv(livekit.LIVEKIT_AGENT_NUM_IDLE_PROCESSES_ENV, "16")
    assert livekit._livekit_agent_server_options()["num_idle_processes"] == 1
