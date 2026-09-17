from types import SimpleNamespace
from openbase_coder_cli.livekit_agent.backend_answer_ownership import preserve_backend_answer_on_cancel
from openbase_coder_cli.livekit_agent.voice_delivery import VoiceDeliveryLedger, VoiceRouteSnapshot


def test_cancelled_consumer_preserves_backend_answer_and_cannot_unmute():
    events = []
    client = SimpleNamespace(pending_voice_answer_turn_id="backend-one")
    ledger = VoiceDeliveryLedger(route_snapshot=lambda: VoiceRouteSnapshot(0, "dispatcher", None, None, "dispatcher"))
    ledger.set_lifecycle_sink(lambda event, record, reason: events.append(event))
    record = ledger.accept_utterance(message_id="one", prompt="tell the whole story")
    ledger._lifecycle_mute_outstanding = True
    stream = SimpleNamespace(_backend_committed=True, _backend_voice_client=client)
    assert preserve_backend_answer_on_cancel(stream, record, ledger)
    assert ledger.record_for_turn("backend-one") is record
    assert ledger.has_pending_delivery_for_current_route()
    assert not ledger._may_release_unmute()
    assert "safe_to_unmute" not in events


def test_unsubmitted_input_or_a_failed_backend_cannot_create_a_phantom_answer_hold():
    ledger = SimpleNamespace(mark_answer_owed=lambda **kwargs: (_ for _ in ()).throw(AssertionError()))
    assert not preserve_backend_answer_on_cancel(SimpleNamespace(_backend_committed=False), object(), ledger)
    stream = SimpleNamespace(_backend_committed=True,
        _backend_voice_client=SimpleNamespace(pending_voice_answer_turn_id=None))
    assert not preserve_backend_answer_on_cancel(stream, object(), ledger)
