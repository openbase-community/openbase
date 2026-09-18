"""Background speech must not steal the user's correction window."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from openbase_coder_cli.livekit_agent.speech_queue import AnnouncerSpeechQueue
from openbase_coder_cli.livekit_agent.turn_detection import UserTurnClosureDecision
from openbase_coder_cli.livekit_agent.voice_delivery import VoiceDeliveryLedger, VoiceRouteSnapshot


def queue_for(session, *, grace=0, ledger=None):
    return AnnouncerSpeechQueue(session=session, announcer_tts=None,
        silence_grace_seconds=grace, delivery_ledger=ledger)


def wait_for_queue(queue):
    return asyncio.create_task(queue._wait_until_both_silent(
        message_id="background", enqueued_at=time.monotonic()))


@pytest.mark.asyncio
async def test_unrelated_state_notification_does_not_shorten_silence():
    session = SimpleNamespace(current_speech=None, user_state="listening")
    queue = queue_for(session, grace=.12)
    started = time.monotonic()
    waiting = wait_for_queue(queue)
    await asyncio.sleep(.02)
    queue.notify_state_changed()
    await asyncio.sleep(.02)
    assert not waiting.done()
    assert await asyncio.wait_for(waiting, 1)
    assert time.monotonic() - started >= .12


@pytest.mark.asyncio
async def test_brief_speech_between_wakeups_restarts_full_grace():
    session = SimpleNamespace(current_speech=None, user_state="listening")
    queue = queue_for(session, grace=.08)
    waiting = wait_for_queue(queue)
    await asyncio.sleep(.06)
    session.user_state = "speaking"
    queue.notify_state_changed()
    session.user_state = "listening"
    queue.notify_state_changed()
    restarted = time.monotonic()
    await asyncio.sleep(.03)
    assert not waiting.done()
    assert await asyncio.wait_for(waiting, 1)
    assert time.monotonic() - restarted >= .08


@pytest.mark.asyncio
@pytest.mark.parametrize("has_transcript", [False, True])
async def test_announcements_wait_for_shared_user_quiet_verification(has_transcript):
    session = SimpleNamespace(current_speech=None, user_state="listening")
    ledger = VoiceDeliveryLedger(route_snapshot=lambda: VoiceRouteSnapshot(
        route_version=0, active_thread_id="dispatcher", active_voice_id=None,
        active_voice_name=None, active_route="dispatcher"),
        vad_quiet_grace_seconds=.12, user_speaking_poll_seconds=.005)
    ledger.set_user_speaking_provider(lambda: session.user_state == "speaking")
    records = []
    ledger.set_lifecycle_sink(lambda event, record, reason: records.append(record))
    if has_transcript:
        record = ledger.accept_utterance(message_id="user", prompt="And one more thing")
        ledger.schedule_user_turn_closure(record, UserTurnClosureDecision(
            confidence=None, source="test", quiet_grace_seconds=.12,
            completion_reason="quiet_floor"))
    else:
        ledger.notify_user_state(new_state="listening", old_state="speaking")
    queue = queue_for(session, ledger=ledger)
    waiting = wait_for_queue(queue)
    await asyncio.sleep(.02)
    assert ledger.user_quiet_verification_pending()
    assert not waiting.done()
    assert await asyncio.wait_for(waiting, 1)
    assert records[-1].user_turn_closed
    ledger.mark_cancelled(records[-1], reason="test_finished")
    ledger._provisional_mute_recovery.cancel()
