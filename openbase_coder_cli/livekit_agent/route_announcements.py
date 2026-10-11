"""Pre-recorded route announcements for GPT-Live calls.

The Classic pipeline speaks a transfer ("You're now talking with …") and a
return ("Back to dispatch.") through its announcer TTS. A GPT-Live call has
no announcer TTS, and its characters only greet on first contact, so a
transfer or return was silent (Gabe, 2026-10-10). These bundled clips play
through the agent's own audio track, right after the character handoff and
before any greeting, so the phone's echo cancellation treats them like any
other agent speech. No client setting gates them: Classic has none either
(the apps' "verbose audio" switch only logs playback diagnostics).
"""

from __future__ import annotations

import logging
from importlib.resources import files
from pathlib import Path

from livekit.agents import AgentSession

from openbase_coder_cli.livekit_agent.speech_queue import audio_file_frames

logger = logging.getLogger(__name__)

SOUNDS_PACKAGE = "openbase_coder_cli.resources.sounds"
ROUTE_TRANSFERRED_SOUND = "voice-route-transferred.wav"
BACK_TO_DISPATCH_SOUND = "back-to-dispatch.wav"


def route_announcement_path(action: str | None, *, announce: bool = True) -> Path | None:
    """The clip for a route move, or None when nothing is announced.

    Mirrors Classic: a return always says "Back to dispatch"; a transfer is
    announced only when its command asks (``announce`` is False when the
    agent that requested the transfer confirms it in its own words, as the
    ``openbase-coder user transfer-to-agent`` command does).
    """
    if action == "exit_to_dispatch":
        name = BACK_TO_DISPATCH_SOUND
    elif action == "transfer_to_thread" and announce:
        name = ROUTE_TRANSFERRED_SOUND
    else:
        return None
    return Path(str(files(SOUNDS_PACKAGE).joinpath(name)))


async def play_route_announcement(session: AgentSession, path: Path) -> bool:
    """Play ``path`` through the session's audio output; True when it played out."""
    handle = session.say(
        "",
        audio=audio_file_frames(session, path),
        allow_interruptions=False,
        add_to_chat_ctx=False,
    )
    await handle.wait_for_playout()
    return not getattr(handle, "interrupted", False)
