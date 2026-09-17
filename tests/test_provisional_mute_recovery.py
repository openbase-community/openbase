import asyncio

from openbase_coder_cli.livekit_agent.voice_delivery import VoiceDeliveryLedger, VoiceRouteSnapshot


def route():
    return VoiceRouteSnapshot(0, "dispatcher", None, None, "dispatcher")


def test_missing_transcript_releases_mute_and_stops_keepalives(monkeypatch):
    from openbase_coder_cli.livekit_agent import voice_delivery
    monkeypatch.setattr(voice_delivery, "MUTE_KEEPALIVE_INTERVAL_SECONDS", 0.01)

    async def run():
        events = []
        ledger = VoiceDeliveryLedger(route_snapshot=route, vad_transcript_timeout_seconds=0.035)
        ledger.set_lifecycle_sink(lambda event, record, reason: events.append((event, record.delivery_id, reason)))
        ledger._emit_vad_quiet_mute()
        await asyncio.sleep(0.06)
        assert events[-1][0] == "safe_to_unmute"
        assert events[-1][1] == events[0][1]
        assert events[-1][2] == "vad_transcript_timeout"
        count = len(events)
        await asyncio.sleep(0.035)
        assert len(events) == count
    asyncio.run(run())


def test_transcript_adoption_does_not_timeout_an_active_backend_turn():
    async def run():
        events = []
        ledger = VoiceDeliveryLedger(route_snapshot=route, vad_transcript_timeout_seconds=0.025)
        ledger.set_lifecycle_sink(lambda event, _record, _reason: events.append(event))
        ledger._emit_vad_quiet_mute()
        ledger.accept_utterance(message_id="one", prompt="build two games")
        await asyncio.sleep(0.06)
        assert "safe_to_unmute" not in events
    asyncio.run(run())


def test_pending_announcement_defers_provisional_recovery():
    async def run():
        events = []
        pending = True
        ledger = VoiceDeliveryLedger(route_snapshot=route, vad_transcript_timeout_seconds=0.025)
        ledger.set_announcement_pending_provider(lambda: pending)
        ledger.set_lifecycle_sink(lambda event, _record, _reason: events.append(event))
        ledger._emit_vad_quiet_mute()
        await asyncio.sleep(0.06)
        assert "safe_to_unmute" not in events
        pending = False
        await asyncio.sleep(0.26)
        assert events[-1] == "safe_to_unmute"
    asyncio.run(run())


def test_late_final_preserves_mute_during_adoption_but_dropped_handoff_is_bounded():
    async def run():
        events = []
        ledger = VoiceDeliveryLedger(route_snapshot=route, vad_transcript_timeout_seconds=0.05)
        ledger.set_lifecycle_sink(lambda event, record, reason: events.append((event, reason)))
        ledger._emit_vad_quiet_mute()
        await asyncio.sleep(0.035)
        ledger.notify_final_transcript()
        await asyncio.sleep(0.025)
        assert not any(event == "safe_to_unmute" for event, _ in events)
        await asyncio.sleep(0.045)
        assert events[-1] == ("safe_to_unmute", "vad_transcript_handoff_timeout")
    asyncio.run(run())


def test_late_final_adopted_by_backend_cannot_release_its_hold():
    async def run():
        events = []
        ledger = VoiceDeliveryLedger(route_snapshot=route, vad_transcript_timeout_seconds=0.025)
        ledger.set_lifecycle_sink(lambda event, _record, _reason: events.append(event))
        ledger._emit_vad_quiet_mute()
        ledger.notify_final_transcript()
        ledger.accept_utterance(message_id="late", prompt="finish speaking")
        ledger.notify_final_transcript()
        await asyncio.sleep(0.06)
        assert "safe_to_unmute" not in events
    asyncio.run(run())
