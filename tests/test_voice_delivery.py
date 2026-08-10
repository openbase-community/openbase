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


def test_zero_audio_emits_safe_to_unmute_with_reason():
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

    assert events[-1] == ("safe_to_unmute", "tts_completed_without_audio")


def test_cancelled_accepted_turn_no_longer_blocks_current_route():
    events: list[tuple[str, str]] = []
    ledger = VoiceDeliveryLedger(route_snapshot=_snapshot)
    ledger.set_lifecycle_sink(
        lambda event, _record, reason: events.append((event, reason))
    )
    record = ledger.accept_utterance(message_id="m1", prompt="interrupted")

    assert ledger.has_pending_delivery_for_current_route()

    ledger.mark_cancelled(record, reason="livekit_llm_stream_cancelled")

    assert record.status == "cancelled"
    assert not ledger.has_pending_delivery_for_current_route()
    assert events == [
        ("utterance_accepted", ""),
        ("safe_to_unmute", "livekit_llm_stream_cancelled"),
    ]


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

    record = ledger.track_unmatched_tts(tts_text="I'll keep flowing with your thoughts.")
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
        record, audio_events=5, audio_seconds=0.6, role="direct", voice_id=None, voice_name=None
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

    warnings = [r.getMessage() for r in caplog.records if "stt_transcription_delayed" in r.getMessage()]
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

    assert not [r for r in caplog.records if "stt_transcription_delayed" in r.getMessage()]
