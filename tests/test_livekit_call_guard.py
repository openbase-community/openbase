from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import livekit.api as livekit_api
import pytest

from openbase_coder_cli import livekit_announcer


@pytest.mark.parametrize("summary_count", [0, 2])
@pytest.mark.parametrize("has_agent", [False, True])
def test_call_guard_reads_membership_despite_stale_room_count(
    monkeypatch, summary_count, has_agent
):
    participants = [
        livekit_api.ParticipantInfo(
            identity="phone",
            kind=livekit_api.ParticipantInfo.Kind.STANDARD,
            state=livekit_api.ParticipantInfo.State.ACTIVE,
        )
    ]
    if has_agent:
        participants.append(
            livekit_api.ParticipantInfo(
                identity="agent",
                kind=livekit_api.ParticipantInfo.Kind.AGENT,
                state=livekit_api.ParticipantInfo.State.ACTIVE,
            )
        )
    room_api = SimpleNamespace(
        list_rooms=AsyncMock(
            return_value=livekit_api.ListRoomsResponse(
                rooms=[livekit_api.Room(name="new-call", num_participants=summary_count)]
            )
        ),
        list_participants=AsyncMock(
            return_value=livekit_api.ListParticipantsResponse(participants=participants)
        ),
    )
    client = SimpleNamespace(room=room_api, aclose=AsyncMock())
    monkeypatch.setattr(livekit_announcer, "_build_livekit_client", lambda: client)

    assert asyncio.run(livekit_announcer.active_voice_room_exists()) is has_agent
    assert room_api.list_participants.await_args.args[0].room == "new-call"
    client.aclose.assert_awaited_once()
