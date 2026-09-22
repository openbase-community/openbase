import asyncio
from types import SimpleNamespace

from openbase_coder_cli.livekit_agent.transcription_notice import (
    TranscriptionTimeoutNotice,
)
from openbase_coder_cli.livekit_agent.voice_delivery import (
    VoiceDeliveryLedger,
    VoiceRouteSnapshot,
)


def fixture():
    now = [0.0]
    messages = []
    queue = SimpleNamespace(enqueue=lambda message: messages.append(message) or True)
    notice = TranscriptionTimeoutNotice(queue, clock=lambda: now[0])
    record = SimpleNamespace(
        user_speech_seconds=4.0,
        delivery_id="missing",
        route_at_acceptance=SimpleNamespace(active_voice_id="voice"),
    )
    return notice, now, messages, record


def test_missing_sustained_speech_gets_a_bounded_notice_not_task_replay():
    notice, now, messages, record = fixture()
    notice.timed_out(record)
    assert len(messages) == 1
    assert messages[0].voice_id == "voice"
    assert "before repeating" in messages[0].text
    notice.timed_out(record)
    assert len(messages) == 1
    now[0] += 60
    notice.timed_out(record)
    assert len(messages) == 2


def test_brief_noise_does_not_prompt_a_retry():
    notice, now, messages, record = fixture()
    record.user_speech_seconds = 0.2
    notice.timed_out(record)
    assert messages == []


def test_blip_above_provisional_mute_floor_stays_quiet():
    """A ~0.8s VAD trigger clears the provisional-mute floor (0.75s) but is
    routinely a cough, background sound, or echo of the agent's own audio —
    not a lost utterance. Announcing trouble for it is a false alarm (observed
    live 2026-09-22 at 809ms and 876ms); only sustained speech may announce."""
    notice, now, messages, record = fixture()
    record.user_speech_seconds = 0.9
    notice.timed_out(record)
    assert messages == []


def test_only_missing_transcript_timeout_not_handoff_failure_notifies():
    async def run():
        for received_final in (False, True):
            notices = []
            events = []
            ledger = VoiceDeliveryLedger(
                route_snapshot=lambda: VoiceRouteSnapshot(
                    0, "dispatcher", None, None, "dispatcher"
                ),
                vad_transcript_timeout_seconds=0.015,
            )
            ledger.set_lifecycle_sink(
                lambda event, record, reason: events.append(event)
            )
            ledger.set_transcript_timeout_sink(notices.append)
            ledger._emit_vad_quiet_mute()
            if received_final:
                ledger.notify_final_transcript()
            await asyncio.sleep(0.04)
            assert events[-1] == "safe_to_unmute"
            assert len(notices) == (0 if received_final else 1)
            await asyncio.sleep(0.02)
            assert len(notices) == (0 if received_final else 1)

    asyncio.run(run())
