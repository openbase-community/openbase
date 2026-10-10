from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openbase_coder_cli.agent_announcements import speech


@pytest.fixture
def transport(monkeypatch):
    session = SimpleNamespace(
        id="thread-worker", agent_name="Rowan", name="review", cwd="/project"
    )
    record = Mock()
    request = Mock(
        return_value=SimpleNamespace(
            json=lambda: {"status": "published", "room_name": "original"}
        )
    )
    notify = Mock()
    monkeypatch.setattr(
        speech,
        "super_agent_voice_for_context",
        lambda *args: SimpleNamespace(voice_id="stable-voice", name="Rowan"),
    )
    monkeypatch.setattr(speech, "record_voice_assignment", record)
    monkeypatch.setattr(speech, "local_server_request", request)
    monkeypatch.setattr(speech, "send_user_say_fallback", notify)
    return SimpleNamespace(
        session=session, record=record, request=request, notify=notify
    )


async def test_uses_exact_worker_identity_and_original_room(transport):
    t = transport
    result = await speech.submit_speech(
        t.session,
        "Rowan: the check passed.",
        "announcer-managed-" + "a" * 32,
        "original",
    )
    assert result["status"] == "published"
    payload = t.request.call_args.kwargs["json"]
    assert payload == {
        "thread_id": "thread-worker",
        "agent_name": "Rowan",
        "text": "Rowan: the check passed.",
        "room_name": "original",
        "message_id": "announcer-managed-" + "a" * 32,
    }
    assert t.record.call_args.kwargs["voice_id"] == "stable-voice"
    t.notify.assert_not_called()


@pytest.mark.parametrize("had_room", [False, True])
async def test_closed_or_absent_original_room_notifies_without_retargeting(
    transport, had_room
):
    t = transport
    t.request.return_value.json = lambda: {"status": "no_active_room"}
    result = await speech.submit_speech(
        t.session, "Rowan: inspected.", "id", "old-room" if had_room else None
    )
    assert result["status"] == "notification_submitted"
    t.notify.assert_called_once_with(
        agent_name="Rowan", message="Rowan: inspected.", thread_id="thread-worker"
    )
    assert t.request.call_count == int(had_room)
    if had_room:
        assert t.request.call_args.kwargs["json"]["room_name"] == "old-room"


async def test_failed_transport_never_claims_submission(transport):
    t = transport
    t.request.return_value.json = lambda: {
        "status": "publish_failed",
        "detail": "Unavailable",
    }
    with pytest.raises(RuntimeError, match="Unavailable"):
        await speech.submit_speech(t.session, "Rowan: inspected.", "id", "original")
    t.notify.assert_not_called()
