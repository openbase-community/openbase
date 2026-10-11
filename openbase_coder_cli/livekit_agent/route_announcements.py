"""Spoken route announcements for GPT-Live calls.

A GPT-Live call's characters only greet on first contact, so a transfer or a
return was silent (Gabe, 2026-10-10). Every transfer now says "Voice route
transferred." and every return "Back to dispatch.", in the one neutral
Cartesia announcer voice Classic uses for its notices, synthesized at
runtime through the same announcer TTS (Openbase Cloud's audio proxy or a
local key, never a bundled recording). Gabe chose these words, a single
voice (an agent's Cartesia voice is not the voice it speaks with on
GPT-Live, so naming it in that voice sounded wrong) and an announcement on
spoken transfers too. The audio plays through the agent's own track right
after the character handoff and before any greeting, so the phone's echo
cancellation treats it like any other agent speech. No client setting gates
it: the apps' "verbose audio" switch only logs playback diagnostics.
"""

from __future__ import annotations

import logging
import uuid

from livekit.agents import AgentSession

from openbase_coder_cli.livekit_agent.announcement_audio import (
    AnnouncementSynthesisOutcome,
    announcement_audio,
)
from openbase_coder_cli.livekit_agent.packets import AnnouncerMessage
from openbase_coder_cli.livekit_agent.tts_selection import VoiceSelectingTTS

logger = logging.getLogger(__name__)

BACK_TO_DISPATCH_TEXT = "Back to dispatch."
TRANSFERRED_TEXT = "Voice route transferred."


class RouteAnnouncer:
    """Builds and speaks the announcement for a route move."""

    def __init__(self, *, tts: VoiceSelectingTTS) -> None:
        self._tts = tts

    def message_for(
        self,
        action: str | None,
        *,
        announce: bool = True,
        agent_label: str | None = None,
    ) -> AnnouncerMessage | None:
        """The words for a route move, or None when it is not one.

        ``announce`` and ``agent_label`` are accepted for the route command's
        shape but do not change the words: every transfer is announced, in
        the neutral announcer voice (``voice_id`` None resolves to it).
        """
        del announce, agent_label
        if action == "exit_to_dispatch":
            text = BACK_TO_DISPATCH_TEXT
        elif action == "transfer_to_thread":
            text = TRANSFERRED_TEXT
        else:
            return None
        return AnnouncerMessage(message_id=f"voice-route-{uuid.uuid4().hex}", text=text)

    async def announce(
        self,
        session: AgentSession,
        action: str | None,
        *,
        announce: bool = True,
        agent_label: str | None = None,
    ) -> bool:
        """Speak the move through ``session``'s output; True when it played out."""
        message = self.message_for(action, announce=announce, agent_label=agent_label)
        if message is None:
            return False
        outcome = AnnouncementSynthesisOutcome()
        handle = session.say(
            message.text,
            audio=announcement_audio(
                self._tts, message.text, voice_id=message.voice_id, outcome=outcome
            ),
            # GPT-Live's server-side turn detection rejects
            # allow_interruptions=False (the SDK warns and ignores it); the
            # speech gate already treats this playout as agent audio, so the
            # announcer's own echo does not count as the caller interrupting.
            add_to_chat_ctx=False,
        )
        await handle.wait_for_playout()
        played = outcome.completed and not getattr(handle, "interrupted", False)
        logger.info(
            "dispatch_timing stage=live_route_announcement message_id=%s action=%s "
            "voice_id=%s audio_events=%d audio_seconds=%.2f played=%s",
            message.message_id,
            action,
            self._tts.resolve_voice_id(message.voice_id),
            outcome.audio_events,
            outcome.audio_seconds,
            played,
        )
        return played
