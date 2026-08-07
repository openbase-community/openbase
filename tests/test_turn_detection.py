from __future__ import annotations

import asyncio

from livekit.agents import llm
from livekit.agents.llm.chat_context import ChatMessage

from openbase_coder_cli.livekit_agent.turn_detection import (
    LOW_CONFIDENCE_USER_TURN_QUIET_GRACE_SECONDS,
    MIN_USER_TURN_QUIET_GRACE_SECONDS,
    SafeMultilingualModel,
    UserTurnClosureSignals,
    VoiceTurnSignalTracker,
    decide_user_turn_closure,
    latest_user_turn_signals_from_chat_ctx,
)


def test_safe_multilingual_model_falls_back_on_assertion(monkeypatch):
    async def fail_prediction(self, chat_ctx, *, timeout=3):
        raise AssertionError(
            "end_of_utterance prediction should always returns a result"
        )

    monkeypatch.setattr(
        "livekit.plugins.turn_detector.multilingual.MultilingualModel.predict_end_of_turn",
        fail_prediction,
    )
    model = object.__new__(SafeMultilingualModel)

    result = asyncio.run(model.predict_end_of_turn(chat_ctx=object()))

    assert result == 1.0


def test_user_turn_closure_uses_minimum_quiet_floor_by_default():
    decision = decide_user_turn_closure()

    assert decision.source == "quiet_floor_default"
    assert decision.completion_reason == "quiet_floor"
    assert decision.quiet_grace_seconds == MIN_USER_TURN_QUIET_GRACE_SECONDS
    assert decision.quiet_grace_seconds >= 2.0


def test_user_turn_closure_extends_quiet_floor_on_low_confidence():
    low = decide_user_turn_closure(signals=UserTurnClosureSignals(eou_probability=0.2))
    high = decide_user_turn_closure(signals=UserTurnClosureSignals(eou_probability=0.9))

    assert low.source == "turn_detector"
    assert low.completion_reason == "low_confidence_quiet_floor"
    assert low.quiet_grace_seconds == LOW_CONFIDENCE_USER_TURN_QUIET_GRACE_SECONDS
    assert low.quiet_grace_seconds >= 4.0
    assert high.source == "turn_detector"
    assert high.completion_reason == "quiet_floor"
    assert high.quiet_grace_seconds == MIN_USER_TURN_QUIET_GRACE_SECONDS
    assert low.quiet_grace_seconds > high.quiet_grace_seconds


def test_user_turn_closure_never_shrinks_below_minimum_floor():
    confident = decide_user_turn_closure(
        signals=UserTurnClosureSignals(eou_probability=0.99),
    )

    assert confident.quiet_grace_seconds >= MIN_USER_TURN_QUIET_GRACE_SECONDS


def test_latest_user_turn_signals_reads_chat_message_metrics_and_detector_signal():
    tracker = VoiceTurnSignalTracker()
    tracker.record_prediction(
        transcript="What is the capital of Alaska?", probability=0.92
    )
    message = ChatMessage(
        role="user",
        content=["What is the capital of Alaska?"],
        transcript_confidence=0.86,
    )
    message.metrics = {
        "started_speaking_at": 100.0,
        "stopped_speaking_at": 102.0,
        "transcription_delay": 0.25,
        "end_of_turn_delay": 0.8,
    }
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.insert(message)

    signals = latest_user_turn_signals_from_chat_ctx(
        chat_ctx,
        turn_signal_tracker=tracker,
    )

    assert signals.eou_probability == 0.92
    assert signals.transcript_confidence == 0.86
    assert signals.started_speaking_at == 100.0
    assert signals.stopped_speaking_at == 102.0
    assert signals.transcription_delay == 0.25
    assert signals.end_of_turn_delay == 0.8
    assert signals.silence_ms == 800
    assert tracker.consume_prediction("What is the capital of Alaska?") is None


def test_safe_multilingual_model_records_turn_detector_prediction(monkeypatch):
    async def predict(self, chat_ctx, *, timeout=3):
        return 0.88

    monkeypatch.setattr(
        "livekit.plugins.turn_detector.multilingual.MultilingualModel.predict_end_of_turn",
        predict,
    )
    tracker = VoiceTurnSignalTracker()
    model = object.__new__(SafeMultilingualModel)
    model._turn_signal_tracker = tracker
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content=["hello there"])

    result = asyncio.run(model.predict_end_of_turn(chat_ctx=chat_ctx))

    assert result == 0.88
    assert tracker.consume_prediction("hello there") == 0.88
