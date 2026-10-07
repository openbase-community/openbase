"""Live voice-call detection for the idle heartbeat.

A workspace on a call is busy even when no coding run is active, so the
cloud heartbeat must not let it idle-stop (or, on Maritime, idle-sleep) mid
conversation. The LiveKit server is the source of truth: a room with a human
participant in it is a live call.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

ROOM_LIST_TIMEOUT_SECONDS = 3.0


def _livekit_http_url() -> str:
    url = os.environ.get("LIVEKIT_URL", "ws://localhost:7880").strip()
    return url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)


async def _count_rooms_with_humans() -> int:
    import livekit.api as livekit_api

    api_key = os.environ.get("LIVEKIT_API_KEY", "").strip()
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "").strip()
    if not api_key or not api_secret:
        return 0
    client = livekit_api.LiveKitAPI(_livekit_http_url(), api_key, api_secret)
    try:
        rooms = await client.room.list_rooms(livekit_api.ListRoomsRequest())
        count = 0
        for room in rooms.rooms:
            participants = await client.room.list_participants(
                livekit_api.ListParticipantsRequest(room=room.name)
            )
            # Agents are participants too; only a non-agent (the caller)
            # makes the room a live call.
            if any(
                participant.kind != livekit_api.ParticipantInfo.Kind.AGENT
                for participant in participants.participants
            ):
                count += 1
        return count
    finally:
        await client.aclose()


def count_active_voice_calls() -> int:
    """Rooms with a human caller right now; 0 when LiveKit cannot be asked.

    Any failure (server down, credentials missing, timeout) is reported as no
    call: the heartbeat samples again shortly, and a wrong "busy" would keep
    a workspace awake for nothing.
    """
    try:
        return asyncio.run(
            asyncio.wait_for(_count_rooms_with_humans(), ROOM_LIST_TIMEOUT_SECONDS)
        )
    except Exception:  # noqa: BLE001 - activity sampling must never break the API
        logger.debug(
            "LiveKit room listing failed; counting no active calls", exc_info=True
        )
        return 0
