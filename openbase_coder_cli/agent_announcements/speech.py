"""Submit protocol speech through the same endpoint as `user say`."""

from __future__ import annotations

import asyncio

from openbase_coder_cli.cli.local_server import local_server_request
from openbase_coder_cli.config.cloud_notifications import send_user_say_fallback
from openbase_coder_cli.livekit_voice_history import record_voice_assignment
from openbase_coder_cli.livekit_voice_route import super_agent_voice_for_context


def _submit(session, text: str, message_id: str, room_name: str | None) -> dict:
    if room_name is None:
        send_user_say_fallback(
            agent_name=session.agent_name, message=text, thread_id=session.id
        )
        return {"status": "notification_submitted", "message_id": message_id}
    voice = super_agent_voice_for_context(session.id, session.name, session.agent_name)
    if voice is None or not session.agent_name:
        raise ValueError("This worker has no resolved speaking identity.")
    record_voice_assignment(
        thread_id=session.id,
        agent_name=session.agent_name,
        cwd=session.cwd,
        voice_id=voice.voice_id,
        voice_name=voice.name,
        kind="super_agent",
        source="agent_announcement_protocol",
    )
    payload = {
        "thread_id": session.id,
        "agent_name": session.agent_name,
        "text": text,
        "message_id": message_id,
    }
    if room_name:
        payload["room_name"] = room_name
    response = local_server_request(
        "POST", "/api/user/say/", json=payload, ok_statuses=(502,), timeout=30
    )
    result = response.json()
    if result.get("status") == "published":
        return result
    if result.get("status") == "no_active_room":
        # Never retarget a completion to a new call after its original room closed.
        send_user_say_fallback(
            agent_name=session.agent_name, message=text, thread_id=session.id
        )
        return {"status": "notification_submitted", "message_id": message_id}
    raise RuntimeError(result.get("detail") or "Announcement submission failed.")


async def submit_speech(session, text, message_id, room_name=None):
    return await asyncio.to_thread(_submit, session, text, message_id, room_name)
