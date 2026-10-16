import asyncio
from unittest.mock import AsyncMock, Mock

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
    old_generation = runner._generation
    runner.run(b"three")
    runner._release_if_idle(old_generation)
    assert runner._runner is not None
    runner._release_if_idle(runner._generation)
    assert runner._runner is None
    runner.run(b"after idle")
    assert initialize.call_count == 2
    runner._release_if_idle(runner._generation)


def test_container_caps_stale_pool_override(monkeypatch):
    from openbase_coder_cli.livekit_agent import livekit

    monkeypatch.setenv("OPENBASE_CODER_SERVICE_SUPERVISOR", "external")
    monkeypatch.setenv(livekit.LIVEKIT_AGENT_NUM_IDLE_PROCESSES_ENV, "16")
    assert livekit._livekit_agent_server_options()["num_idle_processes"] == 1


def test_container_allows_model_rewarm_after_idle(monkeypatch):
    from openbase_coder_cli.livekit_agent import turn_detection

    monkeypatch.setenv("OPENBASE_CODER_SERVICE_SUPERVISOR", "external")
    prediction = AsyncMock(return_value=0.5)
    monkeypatch.setattr(
        turn_detection.MultilingualModel, "predict_end_of_turn", prediction
    )
    now = [0.0]
    monkeypatch.setattr(turn_detection.time, "monotonic", lambda: now[0])
    model = object.__new__(turn_detection.SafeMultilingualModel)
    model._turn_signal_tracker = None
    context = object()
    asyncio.run(model.predict_end_of_turn(context))
    assert prediction.call_args.kwargs["timeout"] == 30
    now[0] = 1.0
    asyncio.run(model.predict_end_of_turn(context))
    assert prediction.call_args.kwargs["timeout"] == 3
    now[0] += lazy_inference.MODEL_IDLE_SECONDS
    asyncio.run(model.predict_end_of_turn(context))
    assert prediction.call_args.kwargs["timeout"] == 30
    now[0] += lazy_inference.MODEL_IDLE_SECONDS
    asyncio.run(model.predict_end_of_turn(context, timeout=None))
    assert prediction.call_args.kwargs["timeout"] is None


def test_desktop_keeps_inference_timeout(monkeypatch):
    from openbase_coder_cli.livekit_agent import turn_detection

    monkeypatch.delenv("OPENBASE_CODER_SERVICE_SUPERVISOR", raising=False)
    prediction = AsyncMock(return_value=0.5)
    monkeypatch.setattr(
        turn_detection.MultilingualModel, "predict_end_of_turn", prediction
    )
    model = object.__new__(turn_detection.SafeMultilingualModel)
    model._turn_signal_tracker = None
    asyncio.run(model.predict_end_of_turn(object()))
    assert prediction.call_args.kwargs["timeout"] == 3
