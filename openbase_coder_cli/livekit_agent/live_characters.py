"""Serialized GPT-Live character sessions within one LiveKit call.

GPT-Live 1.8.4 fixes voice, instructions and startup history at session start.
Use AgentSession.update_agent with a new model, never session.update. Temporary
announcement agents never change the voice router or receive backend results.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

from livekit.agents import Agent, llm

from openbase_coder_cli.voice_identity import agent_voice_identity, route_voice_identity

from .live_delegation import chunk_commentary
from .live_preconnect import wait_live_session_started

logger = logging.getLogger(__name__)


def bounded_history(context):
    """Keep recent textual conversation within GPT-Live startup limits."""
    result = llm.ChatContext()
    kept = []
    remaining = 6000  # UTF-8 bytes bound tokens too, leaving room for framing
    for item in reversed(context.items):
        if not isinstance(item, llm.ChatMessage) or item.role not in {
            "user",
            "assistant",
        }:
            continue
        text = item.text_content or ""
        size = len(text.encode("utf-8"))
        if size > remaining or len(kept) >= 64:
            break
        remaining -= size
        kept.append(item.model_copy(deep=True))
    result.items.extend(reversed(kept))
    return result


class CharacterAssistant(Agent):
    def __init__(self, *, model, instructions, history):
        super().__init__(llm=model, instructions=instructions, chat_ctx=history)
        self.entered = asyncio.Event()

    async def on_enter(self):
        self.entered.set()


class LiveCharacterController:
    """Own session replacement, bounded announcements, and call cleanup."""

    def __init__(
        self,
        *,
        session,
        bridge,
        router,
        model_factory,
        instructions,
        on_error,
        timeout=15.0,
        ledger=None,
        initial_model=None,
    ):
        self.session = session
        self.bridge = bridge
        self.router = router
        self.model_factory = model_factory
        self.instructions = instructions
        self.on_error = on_error
        self.timeout = timeout
        self.ledger = ledger
        self._record = None
        self._identity = None
        self._queue = asyncio.Queue(maxsize=32)
        self._seen = deque(maxlen=256)
        self._task = None
        self._closed = False
        self._route_pending = False
        self._wake = asyncio.Event()
        self._announcement_stop = asyncio.Event()
        self._speech_changed = asyncio.Event()
        self._spoken = False
        self._announcing = False
        self._model = initial_model

    def start(self):
        self._task = asyncio.create_task(self._run(), name="live-characters")

    def route_changed(self):
        # Detach synchronously: no result can enter the old character while
        # the actor waits for the framework to finish its handoff.
        self.bridge.suspend_session()
        self._route_pending = True
        self._announcement_stop.set()
        self._speech_changed.set()
        self._wake.set()

    def announce(self, message):
        if self._closed or message.message_id in self._seen:
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.error(
                "live_character_announcement_queue_full message_id=%s",
                message.message_id,
            )
            return
        self._seen.append(message.message_id)
        self._wake.set()

    def state_changed(self, event):
        if self._announcing and event.new_state == "speaking":
            self._spoken = True
        if self._announcing:
            return self._announcement_state(event)

        self._speech_changed.set()
        self._wake.set()

    @property
    def announcing(self):
        return self._announcing

    def _announcement_state(self, event):
        if self.ledger and self._record and event.new_state == "speaking":
            self.ledger.mark_audio_started(
                self._record,
                latency_ms=0,
                role="announcer",
                voice_id=self._identity.voice_id,
                voice_name=self._identity.voice_name,
            )
        self._speech_changed.set()
        self._wake.set()

    def user_state_changed(self, event):
        if self._announcing and event.new_state == "speaking":
            self._announcement_stop.set()
            self._speech_changed.set()
        self._wake.set()

    async def _replace(self, *, identity, history, instructions):
        await self.session.interrupt()
        model = self.model_factory(identity.gpt_live_voice)
        assistant = CharacterAssistant(
            model=model, instructions=instructions, history=history
        )
        previous = self._model
        self._model = model
        self.session.update_agent(assistant)
        async with asyncio.timeout(self.timeout):
            await assistant.entered.wait()
            await wait_live_session_started(
                assistant.duplex_session, timeout=self.timeout
            )
        if previous is not None:
            await previous.aclose()
        logger.info(
            "dispatch_timing stage=live_character_started voice_id=%s "
            "gpt_live_voice=%s session_id=%s route_thread=%s announcement=%s",
            identity.voice_id,
            identity.gpt_live_voice,
            assistant.duplex_session.session_id,
            self.router.route_snapshot().active_thread_id,
            self._announcing,
        )
        return assistant

    async def _conversation(self, history):
        while not self._closed:
            snapshot = self.router.route_snapshot()
            assistant = await self._replace(
                identity=route_voice_identity(self.router),
                history=history,
                instructions=self.instructions(self.bridge.starting_agent_label()),
            )
            if not self.router.can_deliver_for_snapshot(snapshot):
                continue
            self._route_pending = False
            self.bridge.attach(assistant.duplex_session)
            self.bridge.on_session_reconnected()
            return assistant

    async def _run(self):
        try:
            while not self._closed:
                await self._wake.wait()
                self._wake.clear()
                if self._route_pending:
                    self._route_pending = False
                    await self._conversation(
                        bounded_history(self.session.current_agent.chat_ctx)
                    )
                    self.bridge.announce(f"Hi, I'm {self.bridge.active_agent_label}.")
                    continue
                if self._queue.empty():
                    continue
                # Preserve caller speech and let the current reply finish.
                if (
                    self.session.agent_state == "speaking"
                    or self.session.user_state == "speaking"
                ):
                    continue
                message = self._queue.get_nowait()
                await self._announcement(message)
                if not self._queue.empty():
                    self._wake.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failed handoff must never silently continue in the wrong voice.
            await self.on_error(exc)

    async def _announcement(self, message):
        voice_id = message.voice_id
        if not voice_id and message.agent_name:
            from openbase_coder_cli.livekit_voice_route import (
                super_agent_voice_for_agent_name,
            )

            assigned = super_agent_voice_for_agent_name(message.agent_name)
            if assigned is None:
                raise ValueError(
                    f"No voice assigned to announcing agent {message.agent_name}"
                )
            voice_id = assigned.voice_id
        identity = (
            agent_voice_identity(voice_id)
            if voice_id
            else route_voice_identity(self.router)
        )
        history = bounded_history(self.session.current_agent.chat_ctx)
        self.bridge.suspend_session()
        self._announcement_stop.clear()
        self._spoken = False
        self._announcing = True
        self._identity = identity
        self._record = (
            self.ledger.track_announcement(text=message.text) if self.ledger else None
        )
        completed = False
        received = []
        assistant = None

        def caller_input(event):
            # Caller still talks to the original route while a background
            # character speaks. Results remain held until conversation resumes.
            received.append(event)
            self.bridge._on_input_transcription(event)

        try:
            assistant = await self._replace(
                identity=identity,
                history=llm.ChatContext(),
                instructions=(
                    f"You are {message.agent_name or identity.voice_name}, delivering one background announcement. "
                    "Speak only the supplied commentary, introduce yourself by name, then remain silent. "
                    "Do not answer the caller, improvise, repeat prior speech, or claim the call transferred."
                ),
            )
            assistant.duplex_session.on(
                "input_audio_transcription_completed", caller_input
            )
            for chunk in chunk_commentary(message.text):
                assistant.duplex_session.append_commentary(chunk, delegation_id=None)
            async with asyncio.timeout(45):
                while not self._announcement_stop.is_set():
                    self._speech_changed.clear()
                    if self._spoken and self.session.agent_state != "speaking":
                        try:
                            await asyncio.wait_for(
                                self._speech_changed.wait(), timeout=1.2
                            )
                        except TimeoutError:
                            completed = True
                            break
                    else:
                        await asyncio.wait_for(self._speech_changed.wait(), timeout=12)
            logger.info(
                "dispatch_timing stage=live_character_announcement_end message_id=%s "
                "gpt_live_voice=%s interrupted=%s",
                message.message_id,
                identity.gpt_live_voice,
                self._announcement_stop.is_set(),
            )
        except TimeoutError:
            logger.warning(
                "dispatch_timing stage=live_character_announcement_timeout message_id=%s",
                message.message_id,
            )
        finally:
            if assistant is not None:
                assistant.duplex_session.off(
                    "input_audio_transcription_completed", caller_input
                )
            if self.ledger and self._record:
                if completed:
                    self.ledger.mark_live_audio_finished(self._record)
                else:
                    self.ledger.mark_cancelled(
                        self._record, reason="announcement_interrupted_or_timed_out"
                    )
            self._record = None
            self._announcing = False
            # Only caller text joins the conversation history; the announcement
            # model's temporary persona and output must never leak into it.
            for event in received:
                if event.transcript:
                    history.add_message(role="user", content=event.transcript)
            if not self._closed:
                await self._conversation(bounded_history(history))

    async def close(self):
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.session.aclose()
        if self._model:
            await self._model.aclose()
