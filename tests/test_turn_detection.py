from __future__ import annotations

import asyncio

from livekit.agents import llm
from livekit.agents.llm.chat_context import ChatMessage

from openbase_coder_cli.livekit_agent.turn_detection import (
    SafeMultilingualModel,
    UserTurnClosureSignals,
    VoiceTurnSignalTracker,
    decide_user_turn_closure,
    latest_user_turn_signals_from_chat_ctx,
)


def test_safe_multilingual_model_falls_back_on_assertion(monkeypatch):
    async def fail_prediction(self, chat_ctx, *, timeout=3):
        raise AssertionError("end_of_utterance prediction should always returns a result")

    monkeypatch.setattr(
        "livekit.plugins.turn_detector.multilingual.MultilingualModel.predict_end_of_turn",
        fail_prediction,
    )
    model = object.__new__(SafeMultilingualModel)

    result = asyncio.run(model.predict_end_of_turn(chat_ctx=object()))

    assert result == 1.0


def test_user_turn_closure_keeps_incomplete_tail_open_longer():
    decision = decide_user_turn_closure("I want to update the report and")

    assert decision.source == "semantic_tail"
    assert decision.completion_reason == "continuation_tail"
    assert decision.delay_seconds >= 3.0


def test_user_turn_closure_closes_complete_question_quickly():
    decision = decide_user_turn_closure("What is the capital of Alaska?")

    assert decision.source == "semantic_question"
    assert decision.completion_reason == "complete_question"
    assert decision.delay_seconds < 1.0


def test_user_turn_closure_uses_turn_detector_confidence_when_available():
    low = decide_user_turn_closure("Tell me more about this", eou_probability=0.2)
    high = decide_user_turn_closure("Tell me more about this", eou_probability=0.9)

    assert low.source == "turn_detector"
    assert low.delay_seconds > high.delay_seconds
    assert low.delay_seconds >= 6.0
    assert low.completion_reason == "turn_detector_low_confidence"
    assert high.completion_reason == "turn_detector_high_confidence"


def test_user_turn_closure_uses_livekit_metrics_for_delay_floor():
    decision = decide_user_turn_closure(
        "What is the capital of Alaska?",
        signals=UserTurnClosureSignals(end_of_turn_delay=0.1),
    )

    assert decision.source == "semantic_question+livekit_metrics"
    assert decision.delay_seconds == 0.6


def test_latest_user_turn_signals_reads_chat_message_metrics_and_detector_signal():
    tracker = VoiceTurnSignalTracker()
    tracker.record_prediction(transcript="What is the capital of Alaska?", probability=0.92)
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
