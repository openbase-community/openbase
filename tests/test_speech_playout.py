import asyncio
import time

from livekit.agents.voice.agent_activity import _SpeechHandleContextVar
from livekit.agents.voice.speech_handle import SpeechHandle

from openbase_coder_cli.livekit_agent.speech_playout import bind_interruption
from openbase_coder_cli.livekit_agent.voice_delivery import VoiceDeliveryLedger, VoiceRouteSnapshot


def test_interrupted_sdk_handle_cancels_only_its_old_playout_hold():
    async def run():
        ledger = VoiceDeliveryLedger(route_snapshot=lambda: VoiceRouteSnapshot(0, 'dispatch', None, None, 'dispatcher'))
        events = []
        ledger.set_lifecycle_sink(lambda event, record, reason: events.append((event, record.delivery_id)))
        old = ledger.accept_utterance(message_id='old', prompt='Explain at length')
        old.audio_started_at = time.monotonic() - 2
        old.user_turn_closed = True
        ledger.mark_tts_completed(old, audio_events=10, audio_seconds=86, role='direct', voice_id=None, voice_name=None)
        ledger._emit_lifecycle('safe_to_mute_user', old)
        handle = SpeechHandle.create()
        token = _SpeechHandleContextVar.set(handle)
        try:
            bind_interruption(ledger, old)
        finally:
            _SpeechHandleContextVar.reset(token)
        new = ledger.accept_utterance(message_id='new', prompt='Actually, one sentence')
        handle.interrupt()
        handle._mark_done()
        await asyncio.sleep(.01)
        assert old.terminal_reason == 'sdk_playout_interrupted'
        assert old.audio_seconds < 3
        assert old.delivery_id not in ledger._playout_release_tasks
        assert ledger._remaining_playout_seconds(old) <= 0
        assert new.status == 'utterance_accepted'
        assert not any(event == 'safe_to_unmute' for event, _ in events)
        ledger.mark_tts_completed(old, audio_events=20, audio_seconds=100, role='direct', voice_id=None, voice_name=None)
        assert old.status == 'cancelled'
        assert old.delivery_id not in ledger._playout_release_tasks
        ledger._cancel_mute_keepalive_task()
    asyncio.run(run())


def test_normal_sdk_completion_does_not_cancel_delivery():
    async def run():
        calls = []
        class Ledger:
            mark_playout_interrupted = lambda self, record: calls.append(record)
        handle = SpeechHandle.create()
        token = _SpeechHandleContextVar.set(handle)
        try:
            bind_interruption(Ledger(), 'one')
        finally:
            _SpeechHandleContextVar.reset(token)
        handle._mark_done()
        await asyncio.sleep(.01)
        assert calls == []
    asyncio.run(run())
