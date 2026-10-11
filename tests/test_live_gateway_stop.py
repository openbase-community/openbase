import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openbase_coder_cli.livekit_agent import livekit
from openbase_coder_cli.livekit_agent.live_gateway_stop import gateway_stop_from_error
from openbase_coder_cli.plan_copy import SUBSCRIBE_PRO_LINE


@pytest.mark.parametrize(
    "reason,message",
    [
        (
            "live_voice_idle_timeout",
            "Ending the call since it's been quiet for a while. Call again any time.",
        ),
        (
            "live_voice_max_duration",
            "Live voice calls end after 60 minutes. Start a new call to keep talking.",
        ),
        (
            "live_voice_metering_unavailable",
            "Live voice usage metering is temporarily unavailable, so the call was ended. Try again in a few minutes.",
        ),
        (
            "subscription_required",
            f"Free plan lifetime allowance for this feature is used up. {SUBSCRIBE_PRO_LINE}",
        ),
    ],
)
@pytest.mark.parametrize("legacy", [False, True])
async def test_gateway_reason_reaches_phone_before_room_shutdown(
    monkeypatch, reason, message, legacy
):
    error = {"code": "billing_hard_limit_reached", "message": message}
    if not legacy:
        error["type"] = reason
    stop = gateway_stop_from_error(error)
    events = []

    async def publish(data, **kwargs):
        events.append(json.loads(data))
        assert kwargs["reliable"] is True

    async def delete(room):
        events.append(("deleted", room))

    room = SimpleNamespace(
        name="test-room", local_participant=SimpleNamespace(publish_data=publish)
    )
    ctx = SimpleNamespace(room=room, shutdown=lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(livekit, "_delete_room", delete)
    monkeypatch.setattr(livekit.asyncio, "sleep", AsyncMock())

    # Known stops must not be overwritten by a second account lookup (which
    # may use stale credentials after an account switch or be unavailable).
    def unexpected_lookup(**kwargs):
        pytest.fail("A structured stop must not recheck the account")

    monkeypatch.setattr(
        livekit, "ensure_openbase_cloud_audio_subscription", unexpected_lookup
    )
    await livekit._end_call_after_agent_error(ctx, stop)
    assert events[0]["code"] == reason
    assert events[0]["detail"] == message
    assert events[1:] == [("deleted", "test-room"), {"reason": "agent-error"}]


def test_billing_stop_adds_subscribe_line():
    stop = gateway_stop_from_error(
        {"code": "billing_hard_limit_reached", "message": "Credits used up."}
    )
    assert stop.code == "subscription_required"
    assert str(stop) == f"Credits used up. {SUBSCRIBE_PRO_LINE}"


@pytest.mark.parametrize(
    "error",
    [
        {"code": "gateway_restarting"},
        {"code": "invalid_api_key"},
        {"code": "billing_hard_limit_reached", "type": "unknown"},
    ],
)
def test_other_errors_keep_existing_handling(error):
    assert gateway_stop_from_error(error) is None
