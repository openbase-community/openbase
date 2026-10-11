"""Spoken route announcements for GPT-Live calls, as the Classic pipeline says them.

Classic speaks a transfer ("You're now talking with …", in the agent's voice)
and a return ("Back to dispatch.", in the Dispatcher's voice) through its
announcer TTS. A GPT-Live call's characters only greet on first contact, so
a transfer or return was silent (Gabe, 2026-10-10). The same announcer TTS
(Cartesia, through Openbase Cloud's audio proxy or a local key, never a
bundled recording) now synthesizes the same words at the same moments, and
the audio plays through the agent's own track right after the character
handoff and before any greeting, so the phone's echo cancellation treats it
like any other agent speech. No client setting gates this: Classic has none
either (the apps' "verbose audio" switch only logs playback diagnostics).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

from livekit.agents import AgentSession

from openbase_coder_cli.livekit_agent.announcement_audio import (
    AnnouncementSynthesisOutcome,
    announcement_audio,
)
from openbase_coder_cli.livekit_agent.packets import AnnouncerMessage
from openbase_coder_cli.livekit_agent.tts_selection import VoiceSelectingTTS

logger = logging.getLogger(__name__)

BACK_TO_DISPATCH_TEXT = "Back to dispatch."


def transfer_text(name: str | None) -> str:
    """Classic's transfer confirmation (``voice_routing._transfer_voice_route``)."""
    return f"You're now talking with {name}." if name else "Transferred."


class RouteAnnouncer:
    """Builds and speaks the announcement for a route move."""

    def __init__(
        self,
        *,
        tts: VoiceSelectingTTS,
        voice_router,
        dispatcher_voice_id: Callable[[], str | None],
    ) -> None:
        self._tts = tts
        self._voice_router = voice_router
        self._dispatcher_voice_id = dispatcher_voice_id

    def message_for(
        self,
        action: str | None,
        *,
        announce: bool = True,
        agent_label: str | None = None,
    ) -> AnnouncerMessage | None:
        """The words and voice for a route move, or None when it is silent.

        Mirrors Classic: a return always says "Back to dispatch" as the
        Dispatcher; a transfer is announced in the agent's voice only when
        its command asks (``announce`` is False when the agent that requested
        the transfer confirms it in its own words, as the
        ``openbase-coder user transfer-to-agent`` command does).
        """
        message_id = f"voice-route-{uuid.uuid4().hex}"
        if action == "exit_to_dispatch":
            return AnnouncerMessage(
                message_id=message_id,
                text=BACK_TO_DISPATCH_TEXT,
                voice_id=self._dispatcher_voice_id(),
            )
        if action == "transfer_to_thread" and announce:
            name = (
                getattr(self._voice_router, "active_target_voice_name", None)
                or agent_label
            )
            return AnnouncerMessage(
                message_id=message_id,
                text=transfer_text(name),
                voice_id=getattr(self._voice_router, "active_target_voice_id", None),
            )
        return None

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
            allow_interruptions=False,
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
