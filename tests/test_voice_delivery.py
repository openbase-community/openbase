from __future__ import annotations

import asyncio

from openbase_coder_cli.livekit_agent.tts_selection import (
    SpeechFormattingSynthesizeStream,
    text_for_tts,
)
from openbase_coder_cli.livekit_agent.turn_detection import (
    UserTurnClosureDecision,
    UserTurnClosureSignals,
)
from openbase_coder_cli.livekit_agent.voice_delivery import (
    VoiceDeliveryLedger,
    VoiceRouteSnapshot,
)


class _FakeClient:
    def __init__(self) -> None:
        self.claimed: list[str] = []

    def claim_speech(self, turn_id: str) -> bool:
        if turn_id in self.claimed:
            return False
        self.claimed.append(turn_id)
        return True


class _FakeFrame:
    sample_rate = 24_000
    samples_per_channel = 1_200


class _FakeAudioEvent:
    frame = _FakeFrame()


class _FakeTTSStream:
    def __init__(self, *, events=None) -> None:
        self.pushed_text: list[str] = []
        self.flush_count = 0
        self._events = list(events or [])

    def push_text(self, text: str) -> None:
        self.pushed_text.append(text)

    def flush(self) -> None:
        self.flush_count += 1

    def end_input(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, exc_tb) -> None:
        pass


def _snapshot(version: int = 0, thread_id: str = "dispatcher"):
    return VoiceRouteSnapshot(
        route_version=version,
        active_thread_id=thread_id,
        active_voice_id=None,
        active_voice_name=None,
        active_route="dispatcher" if thread_id == "dispatcher" else "codex_thread",
    )


def test_delivery_is_not_marked_spoken_until_first_audio():
    current_route = _snapshot()
    ledger = VoiceDeliveryLedger(route_snapshot=lambda: current_route)
    client = _FakeClient()
    record = ledger.accept_utterance(message_id="m1", prompt="hello")

    ledger.mark_answer_owed(record, turn_id="turn-1", client=client)
    ledger.mark_text_generated(
        record,
        speech_text="Yes, I am here.",
        tts_text=text_for_tts("Yes, I am here."),
    )
    assert ledger.reserve_for_tts(record)
    assert client.claimed == []
    assert ledger.has_pending_delivery_for_current_route()

    matched = ledger.match_tts_flush(
        tts_text=text_for_tts("Yes, I am here."),
        role="direct",
        voice_id="voice-1",
        voice_name="Corey",
    )
    assert matched is record
    assert client.claimed == []

    ledger.mark_audio_started(
        record,
        latency_ms=120,
        role="direct",
        voice_id="voice-1",
        voice_name="Corey",
    )

    assert client.claimed == ["turn-1"]
    assert not ledger.has_pending_delivery_for_current_route()


def test_lifecycle_events_follow_delivery_ledger_transitions():
    events: list[tuple[str, str, str]] = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(
        lambda event, record, reason: events.append((event, record.delivery_id, reason))
    )
    client = _FakeClient()

    record = ledger.accept_utterance(message_id="m1", prompt="hello")
    ledger.mark_answer_owed(record, turn_id="turn-1", client=client)
    ledger.mark_text_generated(
        record,
        speech_text="Yes.",
        tts_text=text_for_tts("Yes."),
    )
    assert ledger.reserve_for_tts(record)
    assert (
        ledger.match_tts_flush(
            tts_text=text_for_tts("Yes."),
            role="direct",
            voice_id="voice-1",
            voice_name="Corey",
        )
        is record
    )
    ledger.mark_audio_started(
        record,
        latency_ms=120,
        role="direct",
        voice_id="voice-1",
        voice_name="Corey",
    )
    ledger.mark_tts_completed(
        record,
        audio_events=1,
        audio_seconds=0.05,
        role="direct",
        voice_id="voice-1",
        voice_name="Corey",
    )

    assert events == [
        ("utterance_accepted", record.delivery_id, ""),
        ("agent_audio_started", record.delivery_id, ""),
        ("agent_audio_finished", record.delivery_id, ""),
        ("safe_to_unmute", record.delivery_id, ""),
    ]


def test_safe_to_mute_user_lifecycle_uses_turn_closure_decision():
    events: list[tuple[str, str, str]] = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(
        lambda event, record, reason: events.append((event, record.delivery_id, reason))
    )
    record = ledger.accept_utterance(message_id="m1", prompt="What is the capital?")
    decision = UserTurnClosureDecision(
        confidence=0.8,
        source="turn_detector",
        quiet_grace_seconds=0,
        completion_reason="quiet_floor",
    )

    ledger.schedule_user_turn_closure(
        record,
        decision,
        signals=UserTurnClosureSignals(
            eou_probability=0.8,
            transcript_confidence=0.9,
            transcription_delay=0.2,
            end_of_turn_delay=0.7,
        ),
    )

    assert record.user_turn_closed
    assert record.user_turn_closure_source == "turn_detector"
    assert record.user_turn_closure_delay_ms == 0
    assert record.user_turn_eou_probability == 0.8
    assert record.user_turn_silence_ms == 700
    assert record.user_turn_transcript_confidence == 0.9
    assert record.user_turn_transcription_delay_ms == 200
    assert events == [
        ("utterance_accepted", record.delivery_id, ""),
        ("safe_to_mute_user", record.delivery_id, "quiet_floor"),
    ]


def test_superseded_utterance_cancels_pending_safe_to_mute_user():
    async def run() -> tuple[list[tuple[str, str]], VoiceDeliveryRecord]:
        events: list[tuple[str, str]] = []
        ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
        ledger.set_lifecycle_sink(
            lambda event, record, _reason: events.append((event, record.delivery_id))
        )
        stale_record = ledger.accept_utterance(message_id="m1", prompt="I want to")
        ledger.schedule_user_turn_closure(
            stale_record,
            UserTurnClosureDecision(
                confidence=0.35,
                source="turn_detector",
                quiet_grace_seconds=0.05,
                completion_reason="low_confidence_quiet_floor",
            ),
        )
        current_record = ledger.accept_utterance(message_id="m2", prompt="continue")
        await asyncio.sleep(0.08)
        return events, current_record

    events, current_record = asyncio.run(run())

    assert ("safe_to_mute_user", current_record.delivery_id) not in events
    assert all(event != ("safe_to_mute_user", events[0][1]) for event in events)


def test_safe_to_mute_user_waits_while_user_is_still_speaking():
    async def run() -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        is_speaking = True
        ledger = VoiceDeliveryLedger(
            route_snapshot=_snapshot,
            user_speaking_poll_seconds=0.005,
        )
        ledger.set_lifecycle_sink(
            lambda event, record, _reason: events.append((event, record.delivery_id))
        )
        ledger.set_user_speaking_provider(lambda: is_speaking)
        record = ledger.accept_utterance(message_id="m1", prompt="I am still talking")
        ledger.schedule_user_turn_closure(
            record,
            UserTurnClosureDecision(
                confidence=0.2,
                source="turn_detector",
                quiet_grace_seconds=0.005,
                completion_reason="low_confidence_quiet_floor",
            ),
        )
        await asyncio.sleep(0.03)
        assert not record.user_turn_closed
        assert ("safe_to_mute_user", record.delivery_id) not in events

        is_speaking = False
        await asyncio.sleep(0.04)
        assert record.user_turn_closed
        return events

    events = asyncio.run(run())

    assert [event[0] for event in events] == [
        "utterance_accepted",
        "safe_to_mute_user",
    ]


def test_tts_waits_for_user_turn_closure():
    async def run() -> tuple[bool, bool]:
        ledger = VoiceDeliveryLedger(
            route_snapshot=_snapshot,
            user_speaking_poll_seconds=0.005,
        )
        is_speaking = True
        ledger.set_user_speaking_provider(lambda: is_speaking)
        record = ledger.accept_utterance(message_id="m1", prompt="I am still talking")
        ledger.schedule_user_turn_closure(
            record,
            UserTurnClosureDecision(
                confidence=0.2,
                source="turn_detector",
                quiet_grace_seconds=0.005,
                completion_reason="low_confidence_quiet_floor",
            ),
        )
        wait_task = asyncio.create_task(
            ledger.wait_for_user_turn_closed_before_tts(record)
        )
        await asyncio.sleep(0.03)
        was_done_while_speaking = wait_task.done()

        is_speaking = False
        allowed = await asyncio.wait_for(wait_task, timeout=0.2)
        return was_done_while_speaking, allowed

    was_done_while_speaking, allowed = asyncio.run(run())

    assert not was_done_while_speaking
    assert allowed


def test_user_turn_closure_uses_quiet_floor_not_timer_only():
    async def run() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        events: list[tuple[str, str]] = []
        ledger = VoiceDeliveryLedger(
            route_snapshot=_snapshot,
            user_speaking_poll_seconds=0.005,
        )
        ledger.set_lifecycle_sink(
            lambda event, record, _reason: events.append((event, record.delivery_id))
        )
        ledger.set_user_speaking_provider(lambda: False)

        high_record = ledger.accept_utterance(message_id="m1", prompt="What now?")
        ledger.schedule_user_turn_closure(
            high_record,
            UserTurnClosureDecision(
                confidence=0.9,
                source="turn_detector",
                quiet_grace_seconds=0.03,
                completion_reason="quiet_floor",
            ),
        )
        await asyncio.sleep(0.015)
        high_mid_events = list(events)
        await asyncio.sleep(0.03)

        low_record = ledger.accept_utterance(message_id="m2", prompt="I think")
        ledger.schedule_user_turn_closure(
            low_record,
            UserTurnClosureDecision(
                confidence=0.2,
                source="turn_detector",
                quiet_grace_seconds=0.06,
                completion_reason="low_confidence_quiet_floor",
            ),
        )
        await asyncio.sleep(0.04)
        low_mid_events = list(events)
        await asyncio.sleep(0.04)
        return high_mid_events, low_mid_events

    high_mid_events, low_mid_events = asyncio.run(run())

    assert [event[0] for event in high_mid_events] == ["utterance_accepted"]
    assert low_mid_events[-1][0] == "utterance_accepted"


def test_zero_audio_does_not_claim_speech():
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    client = _FakeClient()
    record = ledger.accept_utterance(message_id="m1", prompt="hello")

    ledger.mark_answer_owed(record, turn_id="turn-1", client=client)
    ledger.mark_text_generated(
        record,
        speech_text="Yes, I am here.",
        tts_text=text_for_tts("Yes, I am here."),
    )
    assert ledger.reserve_for_tts(record)
    assert (
        ledger.match_tts_flush(
            tts_text=text_for_tts("Yes, I am here."),
            role="direct",
            voice_id="voice-1",
            voice_name="Corey",
        )
        is record
    )

    ledger.mark_tts_completed(
        record,
        audio_events=0,
        audio_seconds=0.0,
        role="direct",
        voice_id="voice-1",
        voice_name="Corey",
    )

    assert record.status == "zero_audio"
    assert client.claimed == []


def _immediate_closure_decision() -> UserTurnClosureDecision:
    return UserTurnClosureDecision(
        confidence=0.9,
        source="test",
        quiet_grace_seconds=0.0,
        completion_reason="test",
    )


def test_zero_audio_releases_outstanding_mute_with_reason():
    events: list[tuple[str, str]] = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(
        lambda event, _record, reason: events.append((event, reason))
    )
    record = ledger.accept_utterance(message_id="m1", prompt="hello")
    ledger.mark_user_turn_closed(record, decision=_immediate_closure_decision())
    assert ("safe_to_mute_user", "test") in events
    ledger.mark_answer_owed(record, turn_id="turn-1", client=_FakeClient())
    ledger.mark_text_generated(
        record,
        speech_text="Yes.",
        tts_text=text_for_tts("Yes."),
    )
    assert ledger.reserve_for_tts(record)

    ledger.mark_tts_completed(
        record,
        audio_events=0,
        audio_seconds=0.0,
        role="direct",
        voice_id="voice-1",
        voice_name="Corey",
    )

    assert events[-1] == ("safe_to_unmute", "tts_completed_without_audio")


def test_zero_audio_without_outstanding_mute_does_not_unmute():
    events: list[tuple[str, str]] = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(
        lambda event, _record, reason: events.append((event, reason))
    )
    record = ledger.accept_utterance(message_id="m1", prompt="hello")
    ledger.mark_answer_owed(record, turn_id="turn-1", client=_FakeClient())
    ledger.mark_text_generated(
        record,
        speech_text="Yes.",
        tts_text=text_for_tts("Yes."),
    )
    assert ledger.reserve_for_tts(record)

    ledger.mark_tts_completed(
        record,
        audio_events=0,
        audio_seconds=0.0,
        role="direct",
        voice_id="voice-1",
        voice_name="Corey",
    )

    assert all(event != "safe_to_unmute" for event, _reason in events)


def test_cancelled_turn_releases_outstanding_mute():
    events: list[tuple[str, str]] = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(
        lambda event, _record, reason: events.append((event, reason))
    )
    record = ledger.accept_utterance(message_id="m1", prompt="interrupted")
    ledger.mark_user_turn_closed(record, decision=_immediate_closure_decision())

    assert ledger.has_pending_delivery_for_current_route()

    ledger.mark_cancelled(record, reason="livekit_llm_stream_cancelled")

    assert record.status == "cancelled"
    assert not ledger.has_pending_delivery_for_current_route()
    assert events == [
        ("utterance_accepted", ""),
        ("safe_to_mute_user", "test"),
        ("safe_to_unmute", "livekit_llm_stream_cancelled"),
    ]


def test_cancelled_turn_with_open_mic_does_not_unmute():
    """A steered/superseded turn that never spoke must not reopen the mic.

    Spurious safe_to_unmute packets on cancellation were the trigger for the
    iPhone unmuting with no dispatcher audio during Super Agents MCP turns.
    """
    events: list[tuple[str, str]] = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(
        lambda event, _record, reason: events.append((event, reason))
    )
    record = ledger.accept_utterance(message_id="m1", prompt="interrupted")

    ledger.mark_cancelled(record, reason="livekit_llm_stream_cancelled")

    assert record.status == "cancelled"
    assert not ledger.has_pending_delivery_for_current_route()
    assert events == [("utterance_accepted", "")]


def test_cancelled_prior_turn_does_not_block_later_safe_to_unmute():
    events: list[tuple[str, str]] = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(
        lambda event, record, _reason: events.append((event, record.delivery_id))
    )
    stale_record = ledger.accept_utterance(message_id="m1", prompt="interrupted")
    current_record = ledger.accept_utterance(message_id="m2", prompt="onions")

    ledger.mark_cancelled(stale_record, reason="livekit_llm_stream_cancelled")
    assert ledger.has_pending_delivery_for_current_route()
    ledger.mark_tts_completed(
        current_record,
        audio_events=1,
        audio_seconds=0.1,
        role="direct",
        voice_id="voice-1",
        voice_name="Dispatcher",
    )

    assert not ledger.has_pending_delivery_for_current_route()
    assert events[-2:] == [
        ("agent_audio_finished", current_record.delivery_id),
        ("safe_to_unmute", current_record.delivery_id),
    ]


def test_new_utterance_supersedes_reserved_prior_turn_without_stale_unmute():
    events: list[tuple[str, str, str]] = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(
        lambda event, record, reason: events.append((event, record.delivery_id, reason))
    )
    stale_record = ledger.accept_utterance(message_id="m1", prompt="blueberries")
    ledger.mark_answer_owed(stale_record, turn_id="turn-1", client=_FakeClient())
    ledger.mark_text_generated(
        stale_record,
        speech_text="Blueberries are blue.",
        tts_text=text_for_tts("Blueberries are blue."),
    )
    assert ledger.reserve_for_tts(stale_record)

    current_record = ledger.accept_utterance(message_id="m2", prompt="strawberries")

    assert stale_record.status == "cancelled"
    assert stale_record.terminal_reason == "superseded_by_new_utterance"
    assert ledger.has_pending_delivery_for_current_route()
    assert (
        "safe_to_unmute",
        stale_record.delivery_id,
        "superseded_by_new_utterance",
    ) not in events

    ledger.mark_tts_completed(
        current_record,
        audio_events=1,
        audio_seconds=0.1,
        role="direct",
        voice_id="voice-1",
        voice_name="Dispatcher",
    )

    assert not ledger.has_pending_delivery_for_current_route()
    assert events[-2:] == [
        ("agent_audio_finished", current_record.delivery_id, ""),
        ("safe_to_unmute", current_record.delivery_id, ""),
    ]


def test_superseded_delivery_record_cannot_be_revived_by_late_completion():
    events: list[tuple[str, str, str]] = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(
        lambda event, record, reason: events.append((event, record.delivery_id, reason))
    )
    client = _FakeClient()

    stale_record = ledger.accept_utterance(
        message_id="m1",
        prompt="wait then tell me a skeleton joke",
    )
    current_record = ledger.accept_utterance(
        message_id="m2",
        prompt="actually make it about mermaids",
    )

    assert stale_record.status == "cancelled"
    assert stale_record.terminal_reason == "superseded_by_new_utterance"

    assert not ledger.mark_answer_owed(
        stale_record,
        turn_id="turn-1",
        client=client,
    )
    assert not ledger.mark_text_generated(
        stale_record,
        speech_text="Here is the stale skeleton joke.",
        tts_text=text_for_tts("Here is the stale skeleton joke."),
    )
    assert not ledger.reserve_for_tts(stale_record)

    assert stale_record.status == "cancelled"
    assert client.claimed == []
    assert ledger.record_for_turn("turn-1") is None
    assert ledger.has_pending_delivery_for_current_route()

    ledger.mark_tts_completed(
        current_record,
        audio_events=1,
        audio_seconds=0.1,
        role="direct",
        voice_id="voice-1",
        voice_name="Dispatcher",
    )

    assert events[-2:] == [
        ("agent_audio_finished", current_record.delivery_id, ""),
        ("safe_to_unmute", current_record.delivery_id, ""),
    ]


def test_stale_route_suppresses_tts_text_push():
    current_route = _snapshot(version=1, thread_id="corey-thread")
    events: list[str] = []
    ledger = VoiceDeliveryLedger(route_snapshot=lambda: current_route)
    ledger.set_lifecycle_sink(lambda event, _record, _reason: events.append(event))
    record = ledger.accept_utterance(message_id="m1", prompt="question")
    ledger.mark_answer_owed(record, turn_id="turn-1", client=_FakeClient())
    ledger.mark_text_generated(
        record,
        speech_text="Older answer.",
        tts_text=text_for_tts("Older answer."),
    )
    assert ledger.reserve_for_tts(record)
    current_route = _snapshot(version=2, thread_id="dispatcher")

    fake_stream = _FakeTTSStream()
    stream = SpeechFormattingSynthesizeStream(
        fake_stream,
        role="direct",
        voice_id="voice-corey",
        voice_name="Corey",
        delivery_ledger=ledger,
    )
    stream.push_text("Older answer.")
    stream.flush()

    assert fake_stream.pushed_text == []
    assert fake_stream.flush_count == 1
    assert record.status == "suppressed_stale"
    assert events == ["utterance_accepted"]


def test_tts_stream_records_long_audio_delivery_gaps_without_dropping_frames(monkeypatch, caplog):
    from types import SimpleNamespace
    from openbase_coder_cli.livekit_agent import tts_selection
    ticks = iter([100.0, 112.5])
    monkeypatch.setattr(tts_selection, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    events = [_FakeAudioEvent(), _FakeAudioEvent()]
    stream = SpeechFormattingSynthesizeStream(_FakeTTSStream(events=events), role="direct")
    async def drain():
        return [event async for event in stream]
    with caplog.at_level("INFO"):
        assert asyncio.run(drain()) == events
    assert stream._max_audio_event_gap_ms == 12500
    assert "stage=tts_stream_audio_gap" in caplog.text
    assert "max_audio_event_gap_ms=12500.0" in caplog.text


def test_tts_stream_marks_delivery_on_first_audio():
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    client = _FakeClient()
    record = ledger.accept_utterance(message_id="m1", prompt="hello")
    ledger.mark_answer_owed(record, turn_id="turn-1", client=client)
    ledger.mark_text_generated(
        record,
        speech_text="Yes.",
        tts_text=text_for_tts("Yes."),
    )
    assert ledger.reserve_for_tts(record)

    fake_stream = _FakeTTSStream(events=[_FakeAudioEvent()])
    stream = SpeechFormattingSynthesizeStream(
        fake_stream,
        role="direct",
        voice_id="voice-1",
        voice_name="Corey",
        delivery_ledger=ledger,
    )
    stream.push_text("Yes.")
    stream.flush()

    async def drain() -> None:
        async for _event in stream:
            pass

    asyncio.run(drain())

    assert fake_stream.pushed_text == [text_for_tts("Yes.")]
    assert client.claimed == ["turn-1"]
    assert record.status == "audio_delivered"
    assert record.audio_events == 1


def test_tts_flush_fallback_matches_single_sanitized_current_route_candidate():
    events: list[tuple[str, str]] = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(
        lambda event, record, _reason: events.append((event, record.delivery_id))
    )
    client = _FakeClient()
    record = ledger.accept_utterance(
        message_id="m1",
        prompt="What is the capital of Azerbaijan?",
    )
    ledger.mark_answer_owed(record, turn_id="turn-1", client=client)
    ledger.mark_text_generated(
        record,
        speech_text=(
            "The capital of Azerbaijan is Baku. 🇦🇿 It's the largest city "
            "in the country and sits on the coast of the Caspian Sea."
        ),
        tts_text=(
            "The capital of Azerbaijan is Baku. 🇦🇿 It's the largest city "
            "in the country and sits on the coast of the Caspian Sea."
        ),
    )
    assert ledger.reserve_for_tts(record)

    matched = ledger.match_tts_flush(
        tts_text=(
            "The capital of Azerbaijan is Baku. It's the largest city in the "
            "country and sits on the coast of the Caspian Sea."
        ),
        role="direct",
        voice_id="voice-1",
        voice_name="Dispatcher",
    )

    assert matched is record
    assert record.status == "tts_flushed"

    ledger.mark_audio_started(
        record,
        latency_ms=120,
        role="direct",
        voice_id="voice-1",
        voice_name="Dispatcher",
    )
    ledger.mark_tts_completed(
        record,
        audio_events=3,
        audio_seconds=0.15,
        role="direct",
        voice_id="voice-1",
        voice_name="Dispatcher",
    )

    assert client.claimed == ["turn-1"]
    assert record.status == "audio_delivered"
    assert events[-2:] == [
        ("agent_audio_finished", record.delivery_id),
        ("safe_to_unmute", record.delivery_id),
    ]


def test_tts_flush_fallback_rejects_incompatible_single_candidate():
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    record = ledger.accept_utterance(message_id="m1", prompt="one")
    ledger.mark_answer_owed(record, turn_id="turn-1", client=_FakeClient())
    ledger.mark_text_generated(
        record,
        speech_text="The answer is about apples.",
        tts_text="The answer is about apples.",
    )
    assert ledger.reserve_for_tts(record)

    assert (
        ledger.match_tts_flush(
            tts_text="Completely different text about trains.",
            role="direct",
            voice_id="voice-1",
            voice_name="Dispatcher",
        )
        is None
    )
    assert record.status == "text_generated"


def test_unmatched_direct_tts_emits_lifecycle_audio_events():
    """Speech with no accepted-utterance record still gates client unmute.

    Regression: a late steer response reached TTS unmatched, produced no
    lifecycle packets, and iOS unmuted mid-speech on the prior response's
    schedule.
    """
    events: list[tuple[str, str]] = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(
        lambda event, record, _reason: events.append((event, record.delivery_id))
    )

    record = ledger.track_unmatched_tts(
        tts_text="I'll keep flowing with your thoughts."
    )
    ledger.mark_audio_started(
        record,
        latency_ms=120,
        role="direct",
        voice_id="voice-1",
        voice_name="Jacqueline",
    )
    ledger.mark_tts_completed(
        record,
        audio_events=14,
        audio_seconds=2.0,
        role="direct",
        voice_id="voice-1",
        voice_name="Jacqueline",
    )

    assert [event[0] for event in events] == [
        "agent_audio_started",
        "agent_audio_finished",
        "safe_to_unmute",
    ]
    assert all(delivery_id == record.delivery_id for _event, delivery_id in events)
    assert record.delivery_id.startswith("voice-direct-")
    assert record.status == "audio_delivered"


def test_unmatched_direct_tts_withholds_safe_to_unmute_while_answer_pending():
    events: list[tuple[str, str]] = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(
        lambda event, record, _reason: events.append((event, record.delivery_id))
    )
    pending = ledger.accept_utterance(message_id="m1", prompt="what about feature B?")
    ledger.mark_answer_owed(pending, turn_id="turn-1", client=_FakeClient())

    record = ledger.track_unmatched_tts(tts_text="One moment.")
    ledger.mark_audio_started(
        record, latency_ms=90, role="direct", voice_id=None, voice_name=None
    )
    ledger.mark_tts_completed(
        record,
        audio_events=5,
        audio_seconds=0.6,
        role="direct",
        voice_id=None,
        voice_name=None,
    )

    assert ("agent_audio_finished", record.delivery_id) in events
    assert ("safe_to_unmute", record.delivery_id) not in events


def test_user_turn_closure_credits_pre_accept_silence():
    """Silence LiveKit already verified before acceptance counts toward the
    quiet floor, so the mute lands relative to actual end of speech."""

    async def run() -> tuple[float, float]:
        ledger = VoiceDeliveryLedger(
            route_snapshot=_snapshot,
            user_speaking_poll_seconds=0.005,
        )
        ledger.set_user_speaking_provider(lambda: False)

        async def timed_closure(prompt: str, credit: float) -> float:
            # A new utterance is always preceded by renewed user speech,
            # which re-arms the mute (clears the prior turn's coverage).
            ledger.notify_user_state(new_state="speaking")
            record = ledger.accept_utterance(message_id=f"m-{prompt}", prompt=prompt)
            started = asyncio.get_running_loop().time()
            ledger.schedule_user_turn_closure(
                record,
                UserTurnClosureDecision(
                    confidence=0.9,
                    source="turn_detector",
                    quiet_grace_seconds=0.08,
                    completion_reason="quiet_floor",
                ),
                signals=UserTurnClosureSignals(end_of_turn_delay=credit),
            )
            while not record.user_turn_closed:
                await asyncio.sleep(0.005)
            return asyncio.get_running_loop().time() - started

        with_credit = await timed_closure("credited", 0.06)
        without_credit = await timed_closure("uncredited", 0.0)
        return with_credit, without_credit

    with_credit, without_credit = asyncio.run(run())

    assert with_credit < without_credit
    assert without_credit >= 0.08


def test_vad_quiet_floor_mutes_before_any_transcript_exists():
    """The provisional mute fires from VAD end-of-speech alone, so slow STT
    finals no longer hold the mic open for their full delay."""

    async def run() -> list[tuple[str, str, str]]:
        events: list[tuple[str, str, str]] = []
        ledger = VoiceDeliveryLedger(
            route_snapshot=_snapshot,
            user_speaking_poll_seconds=0.005,
            vad_min_speech_seconds=0,  # This test isolates the quiet floor.
            vad_quiet_grace_seconds=0.03,
        )
        ledger.set_lifecycle_sink(
            lambda event, record, reason: events.append(
                (event, record.delivery_id, reason)
            )
        )
        ledger.set_user_speaking_provider(lambda: False)

        ledger.notify_user_state(new_state="speaking")
        ledger.notify_user_state(new_state="listening", old_state="speaking")
        await asyncio.sleep(0.06)
        return events

    events = asyncio.run(run())

    assert len(events) == 1
    event, delivery_id, reason = events[0]
    assert event == "safe_to_mute_user"
    assert delivery_id.startswith("voice-vad-")
    assert reason == "vad_quiet_floor"


def test_vad_quiet_floor_cancelled_when_user_resumes_speaking():
    async def run() -> list[str]:
        events: list[str] = []
        ledger = VoiceDeliveryLedger(
            route_snapshot=_snapshot,
            user_speaking_poll_seconds=0.005,
            vad_min_speech_seconds=0,  # This test isolates the quiet floor.
            vad_quiet_grace_seconds=0.03,
        )
        ledger.set_lifecycle_sink(lambda event, _record, _reason: events.append(event))
        ledger.set_user_speaking_provider(lambda: False)

        ledger.notify_user_state(new_state="speaking")
        ledger.notify_user_state(new_state="listening", old_state="speaking")
        await asyncio.sleep(0.01)
        ledger.notify_user_state(new_state="speaking", old_state="listening")
        await asyncio.sleep(0.06)
        return events

    events = asyncio.run(run())

    assert "safe_to_mute_user" not in events


def test_transcript_closure_adopts_prior_vad_mute_without_reemitting():
    async def run() -> tuple[list[str], VoiceDeliveryRecord]:
        events: list[str] = []
        ledger = VoiceDeliveryLedger(
            route_snapshot=_snapshot,
            user_speaking_poll_seconds=0.005,
            vad_min_speech_seconds=0,  # This test isolates the quiet floor.
            vad_quiet_grace_seconds=0.01,
        )
        ledger.set_lifecycle_sink(lambda event, _record, _reason: events.append(event))
        ledger.set_user_speaking_provider(lambda: False)

        ledger.notify_user_state(new_state="speaking")
        ledger.notify_user_state(new_state="listening", old_state="speaking")
        await asyncio.sleep(0.03)
        assert events.count("safe_to_mute_user") == 1

        record = ledger.accept_utterance(message_id="m1", prompt="slow transcript")
        ledger.schedule_user_turn_closure(
            record,
            UserTurnClosureDecision(
                confidence=0.9,
                source="turn_detector",
                quiet_grace_seconds=0.05,
                completion_reason="quiet_floor",
            ),
        )
        await asyncio.sleep(0.08)
        return events, record

    events, record = asyncio.run(run())

    assert record.user_turn_closed
    assert events.count("safe_to_mute_user") == 1


def test_transcript_closure_supersedes_pending_vad_timer():
    async def run() -> tuple[list[tuple[str, str]], VoiceDeliveryRecord]:
        events: list[tuple[str, str]] = []
        ledger = VoiceDeliveryLedger(
            route_snapshot=_snapshot,
            user_speaking_poll_seconds=0.005,
            vad_min_speech_seconds=0,  # This test isolates the quiet floor.
            vad_quiet_grace_seconds=0.05,
        )
        ledger.set_lifecycle_sink(
            lambda event, record, _reason: events.append((event, record.delivery_id))
        )
        ledger.set_user_speaking_provider(lambda: False)

        ledger.notify_user_state(new_state="speaking")
        ledger.notify_user_state(new_state="listening", old_state="speaking")
        record = ledger.accept_utterance(message_id="m1", prompt="fast transcript")
        ledger.schedule_user_turn_closure(
            record,
            UserTurnClosureDecision(
                confidence=0.9,
                source="turn_detector",
                quiet_grace_seconds=0.01,
                completion_reason="quiet_floor",
            ),
        )
        await asyncio.sleep(0.08)
        return events, record

    events, record = asyncio.run(run())

    mute_events = [event for event in events if event[0] == "safe_to_mute_user"]
    assert mute_events == [("safe_to_mute_user", record.delivery_id)]


def test_safe_to_unmute_rearms_vad_mute_for_next_turn():
    async def run() -> list[str]:
        events: list[str] = []
        ledger = VoiceDeliveryLedger(
            route_snapshot=_snapshot,
            user_speaking_poll_seconds=0.005,
            vad_min_speech_seconds=0,  # This test isolates the quiet floor.
            vad_quiet_grace_seconds=0.01,
        )
        ledger.set_lifecycle_sink(lambda event, _record, _reason: events.append(event))
        ledger.set_user_speaking_provider(lambda: False)

        ledger.notify_user_state(new_state="speaking")
        ledger.notify_user_state(new_state="listening", old_state="speaking")
        await asyncio.sleep(0.03)

        direct = ledger.track_unmatched_tts(tts_text="Reply audio.")
        ledger.mark_audio_started(
            direct, latency_ms=50, role="direct", voice_id=None, voice_name=None
        )
        ledger.mark_tts_completed(
            direct,
            audio_events=2,
            audio_seconds=0.2,
            role="direct",
            voice_id=None,
            voice_name=None,
        )
        await asyncio.sleep(0.25)
        assert events[-1] == "safe_to_unmute"

        ledger.notify_user_state(new_state="speaking")
        ledger.notify_user_state(new_state="listening", old_state="speaking")
        await asyncio.sleep(0.03)
        return events

    events = asyncio.run(run())

    assert events.count("safe_to_mute_user") == 2


def test_vad_gap_restarts_provisional_quiet_floor():
    """Dropped VAD backlog may have hidden speech; the provisional mute must
    re-verify a full quiet floor after the gap."""

    async def run() -> tuple[bool, list[str]]:
        events: list[str] = []
        ledger = VoiceDeliveryLedger(
            route_snapshot=_snapshot,
            user_speaking_poll_seconds=0.005,
            vad_min_speech_seconds=0,  # This test isolates the quiet floor.
            vad_quiet_grace_seconds=0.05,
        )
        ledger.set_lifecycle_sink(lambda event, _record, _reason: events.append(event))
        ledger.set_user_speaking_provider(lambda: False)

        ledger.notify_user_state(new_state="speaking")
        ledger.notify_user_state(new_state="listening", old_state="speaking")
        await asyncio.sleep(0.03)
        ledger.notify_vad_gap(4.2)
        await asyncio.sleep(0.04)
        # The original 0.05s floor has elapsed, but the gap restarted it.
        muted_early = "safe_to_mute_user" in events
        await asyncio.sleep(0.05)
        return muted_early, events

    muted_early, events = asyncio.run(run())

    assert not muted_early
    assert events.count("safe_to_mute_user") == 1


def test_vad_gap_restarts_transcript_quiet_floor():
    async def run() -> tuple[bool, bool]:
        ledger = VoiceDeliveryLedger(
            route_snapshot=_snapshot,
            user_speaking_poll_seconds=0.005,
        )
        ledger.set_user_speaking_provider(lambda: False)
        record = ledger.accept_utterance(message_id="m1", prompt="hello")
        ledger.schedule_user_turn_closure(
            record,
            UserTurnClosureDecision(
                confidence=0.9,
                source="turn_detector",
                quiet_grace_seconds=0.05,
                completion_reason="quiet_floor",
            ),
        )
        await asyncio.sleep(0.03)
        ledger.notify_vad_gap(2.0)
        await asyncio.sleep(0.04)
        closed_early = record.user_turn_closed
        await asyncio.sleep(0.05)
        return closed_early, record.user_turn_closed

    closed_early, closed_eventually = asyncio.run(run())

    assert not closed_early
    assert closed_eventually


def test_slow_transcription_final_logs_loud_warning(caplog):
    import logging

    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    record = ledger.accept_utterance(message_id="m1", prompt="hello")

    with caplog.at_level(logging.WARNING):
        ledger.schedule_user_turn_closure(
            record,
            UserTurnClosureDecision(
                confidence=0.9,
                source="turn_detector",
                quiet_grace_seconds=0,
                completion_reason="quiet_floor",
            ),
            signals=UserTurnClosureSignals(transcription_delay=7.2),
        )

    warnings = [
        r.getMessage()
        for r in caplog.records
        if "stt_transcription_delayed" in r.getMessage()
    ]
    assert warnings
    assert "transcription_delay_ms=7200" in warnings[0]


def test_fast_transcription_final_logs_no_warning(caplog):
    import logging

    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    record = ledger.accept_utterance(message_id="m1", prompt="hello")

    with caplog.at_level(logging.WARNING):
        ledger.schedule_user_turn_closure(
            record,
            UserTurnClosureDecision(
                confidence=0.9,
                source="turn_detector",
                quiet_grace_seconds=0,
                completion_reason="quiet_floor",
            ),
            signals=UserTurnClosureSignals(transcription_delay=0.4),
        )

    assert not [
        r for r in caplog.records if "stt_transcription_delayed" in r.getMessage()
    ]


def test_safe_to_unmute_deferred_until_estimated_playout_end():
    """TTS synthesis outruns playback; the mic release must track playout.

    21s of audio finished synthesizing in ~4s during the incident, and the
    early safe_to_unmute reopened the mic mid-speech.
    """

    async def run() -> tuple[list[str], list[str]]:
        events: list[str] = []
        ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
        ledger.set_lifecycle_sink(lambda event, _record, _reason: events.append(event))

        record = ledger.track_unmatched_tts(tts_text="A long reply.")
        ledger.mark_audio_started(
            record, latency_ms=10, role="direct", voice_id=None, voice_name=None
        )
        ledger.mark_tts_completed(
            record,
            audio_events=4,
            audio_seconds=0.3,
            role="direct",
            voice_id=None,
            voice_name=None,
        )
        immediate = list(events)
        await asyncio.sleep(0.4)
        return immediate, events

    immediate, events = asyncio.run(run())

    assert "agent_audio_finished" not in immediate
    assert "safe_to_unmute" not in immediate
    assert events[-2:] == ["agent_audio_finished", "safe_to_unmute"]


def test_pending_announcement_holds_safe_to_unmute():
    """A queued Super Agent intro must keep the mic muted until it plays."""
    events: list[str] = []
    announcement_pending = True
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(lambda event, _record, _reason: events.append(event))
    ledger.set_announcement_pending_provider(lambda: announcement_pending)

    reply = ledger.track_unmatched_tts(tts_text="Launched the agent.")
    ledger.mark_audio_started(
        reply, latency_ms=10, role="direct", voice_id=None, voice_name=None
    )
    ledger.mark_tts_completed(
        reply,
        audio_events=1,
        audio_seconds=0.0,
        role="direct",
        voice_id=None,
        voice_name=None,
    )
    assert "safe_to_unmute" not in events

    announcement_pending = False
    intro = ledger.track_announcement(text="Hey there, I'm Callie.")
    ledger.mark_audio_started(
        intro, latency_ms=0, role="announcer", voice_id=None, voice_name=None
    )
    ledger.mark_tts_completed(
        intro,
        audio_events=1,
        audio_seconds=0.0,
        role="announcer",
        voice_id=None,
        voice_name=None,
    )
    assert events[-1] == "safe_to_unmute"


def test_steer_receipt_closure_emits_mute_once():
    """Accepted proactive steers mute like accepted utterances, without
    duplicating the mute for rapid follow-up steers."""

    async def run() -> list[str]:
        events: list[str] = []
        ledger = VoiceDeliveryLedger(
            route_snapshot=_snapshot,
            user_speaking_poll_seconds=0.005,
            vad_min_speech_seconds=0,  # This test isolates the quiet floor.
            vad_quiet_grace_seconds=0.01,
        )
        ledger.set_lifecycle_sink(lambda event, _record, _reason: events.append(event))
        ledger.set_user_speaking_provider(lambda: False)

        ledger.schedule_steer_receipt_closure()
        ledger.schedule_steer_receipt_closure()
        for _ in range(200):
            if "safe_to_mute_user" in events:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)
        return events

    events = asyncio.run(run())

    assert events.count("safe_to_mute_user") == 1


def test_mute_keepalive_holds_client_watchdog_during_long_turn(monkeypatch):
    """A sustained mute emits a distinct mute_keepalive event so the iOS
    stuck-muted watchdog does not reopen the mic mid-turn, stops as soon as
    the mute is released, and — because it is NOT a repeated
    safe_to_mute_user — can never re-mute a user who manually unmuted to
    interject (clients ignore unknown events but refresh their staleness
    clock)."""
    from openbase_coder_cli.livekit_agent import voice_delivery as vd

    monkeypatch.setattr(vd, "MUTE_KEEPALIVE_INTERVAL_SECONDS", 0.02)

    async def run() -> tuple[int, int]:
        events: list[str] = []
        ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
        ledger.set_lifecycle_sink(lambda event, _record, _reason: events.append(event))
        ledger.set_user_speaking_provider(lambda: False)

        record = ledger.accept_utterance(message_id="m1", prompt="launch an agent")
        ledger.mark_user_turn_closed(record, decision=_immediate_closure_decision())
        await asyncio.sleep(0.07)
        keepalives_while_held = events.count("mute_keepalive")
        # The real mute is emitted exactly once; keepalives never repeat it.
        assert events.count("safe_to_mute_user") == 1

        ledger.mark_cancelled(record, reason="livekit_llm_stream_cancelled")
        assert events[-1] == "safe_to_unmute"
        settled = events.count("mute_keepalive")
        await asyncio.sleep(0.07)
        return keepalives_while_held, events.count("mute_keepalive") - settled

    keepalives_while_held, keepalives_after_release = asyncio.run(run())

    assert keepalives_while_held >= 3
    assert keepalives_after_release == 0


def test_failed_synthesis_releases_hold_and_preserves_partial_failure():
    events = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(lambda event, record, reason: events.append(event))
    record = ledger.track_announcement(text="A long answer")
    ledger.mark_audio_started(record, latency_ms=1, role="direct", voice_id="voice-1", voice_name="Test")
    ledger.mark_tts_failed(record, audio_events=1, audio_seconds=0.05)
    assert record.status == "failed"
    assert record.terminal_reason == "tts_provider_failed_after_partial_audio"
    assert events[-2:] == ["agent_audio_finished", "safe_to_unmute"]


def test_tts_default_timeout_tolerates_congestion_and_explicit_options_survive():
    from livekit.agents.types import APIConnectOptions, DEFAULT_API_CONNECT_OPTIONS
    from openbase_coder_cli.livekit_agent.tts_selection import tts_connect_options
    assert tts_connect_options(DEFAULT_API_CONNECT_OPTIONS).timeout == 60
    explicit = APIConnectOptions(timeout=2)
    assert tts_connect_options(explicit) is explicit


def test_synthesis_error_cleans_up_delivery_instead_of_leaving_mic_hold():
    from livekit.agents import APITimeoutError
    class FailingStream(_FakeTTSStream):
        async def __anext__(self):
            raise APITimeoutError()
    async def run():
        events = []
        ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
        ledger.set_lifecycle_sink(lambda event, record, reason: events.append(event))
        wrapped = SpeechFormattingSynthesizeStream(FailingStream(), role="direct", delivery_ledger=ledger)
        wrapped.push_text("A complete answer")
        wrapped.flush()
        try:
            await wrapped.__anext__()
        except APITimeoutError:
            pass
        else:
            raise AssertionError("Provider failure must propagate")
        assert events[-2:] == ["agent_audio_finished", "safe_to_unmute"]
        assert not ledger.has_pending_delivery_for_current_route()
    asyncio.run(run())


def test_silent_stream_failure_waits_for_partial_playout_and_never_replays():
    from types import SimpleNamespace
    from openbase_coder_cli.livekit_agent.tts_progress import TTSProgressGuard, TTSStreamStalled

    class HangingStream(_FakeTTSStream):
        async def __anext__(self):
            if self._events:
                return self._events.pop(0)
            await asyncio.Event().wait()

    async def run():
        events = []
        ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
        ledger.set_lifecycle_sink(lambda event, record, reason: events.append(event))
        record = ledger.track_announcement(text="Partial answer")
        frame = SimpleNamespace(sample_rate=24000, samples_per_channel=6000)
        underlying = HangingStream(events=[SimpleNamespace(frame=frame)])
        wrapped = SpeechFormattingSynthesizeStream(underlying, role="direct", delivery_ledger=ledger)
        wrapped._progress = TTSProgressGuard(first_audio_seconds=.1, audio_gap_seconds=.02)
        wrapped.push_text("Partial answer")
        wrapped.flush()
        await wrapped.__anext__()
        try:
            await wrapped.__anext__()
        except TTSStreamStalled as error:
            assert not error.retryable
        else:
            raise AssertionError("Silent stream must terminate")
        assert record.status == "failed"
        assert record.terminal_reason == "tts_stream_stalled"
        assert "safe_to_unmute" not in events
        await asyncio.sleep(.3)
        assert events[-2:] == ["agent_audio_finished", "safe_to_unmute"]
        assert underlying.pushed_text == [text_for_tts("Partial answer")]
        assert events.count("safe_to_unmute") == 1
    asyncio.run(run())


def test_partial_failure_does_not_release_before_estimated_playout_tail():
    async def run():
        events = []
        ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
        ledger.set_lifecycle_sink(lambda event, record, reason: events.append(event))
        record = ledger.track_announcement(text="Partial answer")
        ledger.mark_audio_started(record, latency_ms=1, role="direct", voice_id="v", voice_name="Test")
        ledger.mark_tts_failed(record, audio_events=2, audio_seconds=.08)
        assert "safe_to_unmute" not in events
        await asyncio.sleep(.12)
        assert events[-2:] == ["agent_audio_finished", "safe_to_unmute"]
        assert record.status == "failed"
    asyncio.run(run())


def test_stream_underflow_does_not_count_the_gap_as_played_audio():
    import time
    async def run():
        events = []
        ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
        ledger.set_lifecycle_sink(lambda event, record, reason: events.append(event))
        record = ledger.track_announcement(text="Gapped answer")
        ledger.mark_audio_started(record, latency_ms=1, role="direct", voice_id="v", voice_name="Test")
        now = time.monotonic()
        record.audio_started_at = now - 5
        ledger.mark_audio_frame_queued(record, audio_seconds=.03, queued_at=now-5)
        ledger.mark_audio_frame_queued(record, audio_seconds=.08, queued_at=now)
        ledger.mark_tts_completed(record, audio_events=2, audio_seconds=.11,
            role="direct", voice_id="v", voice_name="Test")
        assert "safe_to_unmute" not in events
        await asyncio.sleep(.03)
        assert "safe_to_unmute" not in events
        await asyncio.sleep(.09)
        assert events[-2:] == ["agent_audio_finished", "safe_to_unmute"]
    asyncio.run(run())


def test_partial_failure_preserves_other_pending_answer_mute():
    events = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(lambda event, record, reason: events.append(event))
    record = ledger.track_announcement(text="Partial answer")
    ledger.mark_audio_started(record, latency_ms=1, role="direct", voice_id="v", voice_name="Test")
    ledger.accept_utterance(message_id="another", prompt="Another pending answer")
    ledger.mark_tts_failed(record, audio_events=1, audio_seconds=.01)
    assert events[-1] == "agent_audio_finished"
    assert "safe_to_unmute" not in events


def test_cancelled_other_work_cannot_release_a_delivered_audio_queue():
    import time
    async def run():
        events = []
        ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
        ledger.set_lifecycle_sink(lambda event, record, reason: events.append(event))
        record = ledger.track_announcement(text="Still queued")
        ledger.mark_audio_started(record, latency_ms=1, role="direct", voice_id="v", voice_name="Test")
        ledger.mark_audio_frame_queued(record, audio_seconds=.12, queued_at=time.monotonic())
        ledger.mark_tts_completed(record, audio_events=1, audio_seconds=.12,
            role="direct", voice_id="v", voice_name="Test")
        other = ledger.accept_utterance(message_id="other", prompt="Cancelled unrelated work")
        ledger.mark_cancelled(other, reason="test_cancel")
        assert "safe_to_unmute" not in events
        await asyncio.sleep(.16)
        assert events[-1] == "safe_to_unmute"
    asyncio.run(run())


def test_brief_vad_noise_does_not_mute_but_recognized_short_request_does():
    async def run():
        events = []
        ledger = VoiceDeliveryLedger(route_snapshot=_snapshot, vad_quiet_grace_seconds=.01, user_speaking_poll_seconds=.005)
        ledger.set_lifecycle_sink(lambda event, record, reason: events.append(event))
        ledger.set_user_speaking_provider(lambda: False)
        # Repeated room clicks must not accumulate into a speech turn.
        for _ in range(5):
            ledger.notify_user_state(new_state="speaking")
            ledger._vad_speech_started_at -= .2
            ledger.notify_user_state(new_state="listening", old_state="speaking")
            assert ledger.user_quiet_verification_pending()
            await asyncio.sleep(.02)
        assert events == []
        record = ledger.accept_utterance(message_id="short", prompt="Yes")
        ledger.schedule_user_turn_closure(record, UserTurnClosureDecision(
            confidence=.9, source="turn_detector", quiet_grace_seconds=.01,
            completion_reason="quiet_floor"))
        await asyncio.sleep(.04)
        assert events == ["utterance_accepted", "safe_to_mute_user"]
    asyncio.run(run())


def test_sustained_vad_speech_keeps_provisional_mute_and_duration():
    async def run():
        records = []
        ledger = VoiceDeliveryLedger(route_snapshot=_snapshot, vad_quiet_grace_seconds=.01, user_speaking_poll_seconds=.005)
        ledger.set_lifecycle_sink(lambda event, record, reason: records.append(record))
        ledger.set_user_speaking_provider(lambda: False)
        ledger.notify_user_state(new_state="speaking")
        ledger._vad_speech_started_at -= .8
        ledger.notify_user_state(new_state="listening", old_state="speaking")
        await asyncio.sleep(.04)
        assert len(records) == 1
        assert records[0].user_speech_seconds >= .8
        ledger.mark_cancelled(records[0], reason="test_finished")
        ledger._provisional_mute_recovery.cancel()
    asyncio.run(run())
