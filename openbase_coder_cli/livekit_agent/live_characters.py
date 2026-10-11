"""Serialized GPT-Live character sessions within one LiveKit call.

GPT-Live 1.8.4 fixes voice, instructions and startup history at session start.
Use AgentSession.update_agent with a new model, never session.update. Temporary
announcement agents never change the voice router or receive backend results.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

from livekit.agents import llm

from openbase_coder_cli.agent_announcements.playback import monitor_speech, speech_guard
from openbase_coder_cli.voice_identity import agent_voice_identity, route_voice_identity

from .config import live_voice_greeting
from .live_announcement import (
    AnnouncementWireEvidence,
    announcement_commands,
    announcement_instructions,
)
from .live_preconnect import wait_live_session_started
from .live_speech_gate import SpeechGatedAgent

logger = logging.getLogger(__name__)


def log_character_started(identity, live, router, *, announcement=False):
    logger.info(
        "dispatch_timing stage=live_character_started voice_id=%s "
        "gpt_live_voice=%s session_id=%s route_thread=%s announcement=%s",
        identity.voice_id,
        identity.gpt_live_voice,
        getattr(live, "session_id", ""),
        router.route_snapshot().active_thread_id,
        announcement,
    )


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
        if text:
            kept.append(item.model_copy(update={"content": [text]}, deep=True))
    result.items.extend(reversed(kept))
    return result


def _open_gateway_early(model):
    """Start the gateway handshake now so it overlaps the framework handoff."""
    preconnect = getattr(model, "preconnect", None)
    if preconnect is None:
        return
    try:
        preconnect()
    except Exception:
        logger.warning(
            "dispatch_timing stage=live_character_preconnect_failed", exc_info=True
        )


async def _close_model(model):
    discard = getattr(model, "discard_preconnected", None)
    if discard is not None:
        await discard()
    await model.aclose()


class CharacterAssistant(SpeechGatedAgent):
    def __init__(
        self, *, model, instructions, history, on_enter=None, speech_gate=None
    ):
        super().__init__(llm=model, instructions=instructions, chat_ctx=history)
        self.entered = asyncio.Event()
        self._entered_callback = on_enter
        self._speech_gate = speech_gate

    async def on_enter(self):
        if self._entered_callback is not None:
            self._entered_callback(self.duplex_session)
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
        timeout=20.0,
        caller_drain_timeout=12.0,
        ledger=None,
        initial_model=None,
        speech_gate=None,
        start_attempts=2,
        announce_route=None,
    ):
        self.session = session
        # Speaks a transfer or return after the handoff, as Classic's announcer
        # does (``RouteAnnouncer.announce``); None keeps route moves silent.
        self.announce_route = announce_route
        self.bridge = bridge
        self.router = router
        self.model_factory = model_factory
        self.instructions = instructions
        self.on_error = on_error
        self.timeout = timeout
        self.start_attempts = max(1, start_attempts)
        self.caller_drain_timeout = caller_drain_timeout
        self.ledger = ledger
        self._record = None
        self._identity = None
        self._queue = asyncio.Queue(maxsize=32)
        self._seen = deque(maxlen=256)
        self._task = None
        self._closed = False
        self._route_pending = False
        # The route move the next handoff announces before any greeting:
        # (action, announce, agent_label); None after a restore, which moves
        # no route.
        self._route_announcement = None
        self._wake = asyncio.Event()
        self._announcement_stop = asyncio.Event()
        self._speech_changed = asyncio.Event()
        self._spoken = False
        self._announcing = False
        self._model = initial_model
        self._muted_output = None
        self._speech_gate = speech_gate
        # The initial session already received its greeting. A return to a
        # known route is continuity, not another first introduction.
        self._introduced_routes = {router.route_snapshot().active_thread_id}

    def _silence(self):
        if self._speech_gate is not None:
            self._speech_gate.revoke()
        if self._muted_output is None:
            self._muted_output = self.session.output.audio_enabled
        self.session.output.set_audio_enabled(False)
        return self.session.interrupt(force=True)

    def _resume_output(self):
        if self._muted_output is not None:
            self.session.output.set_audio_enabled(self._muted_output)
            self._muted_output = None

    def start(self):
        self._task = asyncio.create_task(self._run(), name="live-characters")

    def route_changed(self, action=None, *, announce=True, agent_label=None):
        # Detach synchronously: no result can enter the old character while
        # the actor waits for the framework to finish its handoff.
        self.bridge.suspend_session()
        self._silence()
        self._route_pending = True
        self._route_announcement = (action, announce, agent_label) if action else None
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
            self._silence()
            self._announcement_stop.set()
        self._speech_changed.set()
        self._wake.set()

    async def _replace(self, *, identity, history, instructions, on_enter=None):
        """Hand the call to a fresh GPT-Live session in ``identity``'s voice.

        The call must survive a slow or failed swap: the gateway handshake is
        opened before the framework drains the old agent, the wait is bounded
        by the character-start timeout rather than the start-up preflight, and
        a session that does not come up is replaced by another attempt before
        the failure ends the call.
        """
        await self.session.interrupt()
        previous = self._model
        try:
            for attempt in range(1, self.start_attempts + 1):
                model = self.model_factory(identity.gpt_live_voice)
                self._model = model
                _open_gateway_early(model)
                assistant = CharacterAssistant(
                    model=model,
                    instructions=instructions,
                    history=history,
                    on_enter=on_enter,
                    speech_gate=self._speech_gate,
                )
                try:
                    self.session.update_agent(assistant)
                    async with asyncio.timeout(self.timeout):
                        await assistant.entered.wait()
                        await wait_live_session_started(
                            assistant.duplex_session, timeout=self.timeout
                        )
                except Exception as exc:
                    if self._closed or attempt >= self.start_attempts:
                        raise
                    logger.warning(
                        "dispatch_timing stage=live_character_start_retry "
                        "attempt=%d/%d gpt_live_voice=%s error=%s: %s",
                        attempt,
                        self.start_attempts,
                        identity.gpt_live_voice,
                        type(exc).__name__,
                        exc,
                    )
                    await _close_model(model)
                    continue
                break
        finally:
            if previous is not None:
                await previous.aclose()
        log_character_started(
            identity,
            assistant.duplex_session,
            self.router,
            announcement=self._announcing,
        )
        return assistant

    async def _conversation(self, history, *, route_changed=False):
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
            self.bridge.on_character_session_started()
            self._resume_output()
            await self._announce_route()
            label = self.bridge.starting_agent_label()
            first_contact = snapshot.active_thread_id not in self._introduced_routes
            # Every transfer to an agent greets, also back to one met earlier
            # in the call (VM2, 2026-10-11: the second and third transfers to
            # Cooper were silent after the announcer). A return to the
            # Dispatcher (label None) says nothing here: the route announcer
            # speaks the Classic "Back to dispatch." line.
            if first_contact or (route_changed and label is not None):
                self._introduced_routes.add(snapshot.active_thread_id)
                self.bridge.greet(live_voice_greeting(label))
            return assistant

    async def _announce_route(self):
        """Speak the pending route move through the new character's output."""
        move = self._route_announcement
        self._route_announcement = None
        if move is None or self.announce_route is None:
            return
        action, announce, agent_label = move
        try:
            await self.announce_route(
                self.session, action, announce=announce, agent_label=agent_label
            )
        except Exception:
            # The call goes on without the announcement; a synthesis failure
            # must not end the call through the character loop's error path.
            logger.warning(
                "dispatch_timing stage=live_route_announcement_failed action=%s",
                action,
                exc_info=True,
            )

    async def _run(self):
        try:
            while not self._closed:
                await self._wake.wait()
                self._wake.clear()
                if self._route_pending:
                    self._route_pending = False
                    await self._conversation(
                        bounded_history(self.session.current_agent.chat_ctx),
                        route_changed=True,
                    )
                    if not self._queue.empty():
                        self._wake.set()
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
        guard = await speech_guard(message.message_id)
        if guard and not await asyncio.to_thread(guard.current):
            logger.info("managed_announcement_stale message_id=%s", message.message_id)
            return
        async with monitor_speech(guard, self._cancel_managed_announcement):
            await self._play_announcement(message)

    def _cancel_managed_announcement(self):
        self._announcement_stop.set()
        self._speech_changed.set()

    async def _play_announcement(self, message):
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
        route = self.router.route_snapshot()
        self.bridge.suspend_session()
        self._announcement_stop.clear()
        self._spoken = False
        self._announcing = True
        self._identity = identity
        self._record = (
            self.ledger.track_announcement(text=message.text) if self.ledger else None
        )
        completed = False
        received = {}
        pending = {}
        assistant = None
        input_session = None
        wire_evidence = None

        def caller_input(event):
            if not self.router.can_deliver_for_snapshot(route):
                return
            if event.is_final:
                pending.pop(event.item_id, None)
                received[event.item_id] = event.transcript
            else:
                pending[event.item_id] = event.transcript
            self.bridge._on_input_transcription(event)
            self._speech_changed.set()

        def attach_input(live):
            nonlocal input_session, wire_evidence
            input_session = live
            wire_evidence = AnnouncementWireEvidence(live, message.message_id)
            live.on("input_audio_transcription_completed", caller_input)

        try:
            assistant = await self._replace(
                identity=identity,
                history=llm.ChatContext(),
                instructions=announcement_instructions(
                    message.agent_name or identity.voice_name, message.text
                ),
                on_enter=attach_input,
            )
            async with asyncio.timeout(45):
                for command in announcement_commands(
                    message.agent_name or identity.voice_name, message.text
                ):
                    if self._announcement_stop.is_set():
                        break
                    # Use the documented disclosure/greeting shape: the actual
                    # words are in this command, not only in startup context.
                    self._spoken = False
                    if self._speech_gate is not None:
                        self._speech_gate.authorize()
                    assistant.duplex_session.append_instructions(
                        command, delegation_id=None
                    )
                    await self._wait_announcement_speech()
                completed = not self._announcement_stop.is_set()
            logger.info(
                "dispatch_timing stage=live_character_announcement_end message_id=%s "
                "gpt_live_voice=%s interrupted=%s",
                message.message_id,
                identity.gpt_live_voice,
                self._announcement_stop.is_set(),
            )
        except TimeoutError:
            if assistant is None:
                raise
            logger.warning(
                "dispatch_timing stage=live_character_announcement_timeout message_id=%s",
                message.message_id,
            )
        finally:
            try:
                if not self._closed:
                    await self._silence()
                    try:
                        async with asyncio.timeout(self.caller_drain_timeout):
                            while self.router.can_deliver_for_snapshot(route) and (
                                pending or self.session.user_state == "speaking"
                            ):
                                self._speech_changed.clear()
                                await self._speech_changed.wait()
                    except TimeoutError:
                        for item_id, transcript in pending.items():
                            if self.router.can_deliver_for_snapshot(route):
                                self.bridge.on_user_transcript(
                                    transcript, is_final=True, item_id=item_id
                                )
                                received[item_id] = transcript
            finally:
                if wire_evidence is not None:
                    wire_evidence.close()
                if input_session is not None:
                    input_session.off(
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
            if self.router.can_deliver_for_snapshot(route):
                for transcript in received.values():
                    if transcript:
                        history.add_message(role="user", content=transcript)
            if not self._closed and assistant is not None:
                await self._conversation(bounded_history(history))

    async def _wait_announcement_speech(self):
        while not self._announcement_stop.is_set():
            self._speech_changed.clear()
            if self._spoken and self.session.agent_state != "speaking":
                try:
                    await asyncio.wait_for(self._speech_changed.wait(), timeout=1.2)
                except TimeoutError:
                    return
            elif self._spoken:
                # One speaking transition can cover a long continuous utterance.
                # The 12-second deadline bounds startup, not healthy playback;
                # the enclosing announcement deadline still bounds a stuck SDK.
                await self._speech_changed.wait()
            else:
                await asyncio.wait_for(self._speech_changed.wait(), timeout=12)

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
