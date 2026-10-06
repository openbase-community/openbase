"""GPT-Live client delegation bridge to Super Agent threads.

Under the Live Voice engine the voice model (``GPTLiveModel``,
``delegation="client"``) is the only speaker. When it decides the caller
asked for work it emits ``delegation_created`` (an id and the transcript it
has seen so far, no task text). :class:`LiveDelegationBridge` owns the mapping
from those delegations to Super Agent turns: it rebuilds the prompt from its
own input-transcript buffer, runs the turn on the active voice route
(dispatcher, direct thread, or a transferred thread) exactly as the pipeline
does, and feeds the model ``append_thinking`` (silent context) and
``append_commentary`` (spoken, paraphrased) in sentence-bounded chunks of at
most 500 tokens while the thread works. See ``dev-docs/live-voice.md``.

The bridge also keeps the voice lifecycle contract alive without a TTS path:
``utterance_accepted`` on delegation, ``agent_audio_started`` /
``agent_audio_finished`` from the agent session's speaking state, and never
``safe_to_mute_user`` or ``safe_to_unmute`` (the mic stays open).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from openbase_coder_cli.livekit_agent.config import (
    load_direct_livekit_developer_instructions,
)
from openbase_coder_cli.livekit_agent.speech_formatter import (
    format_for_speech_segments,
)
from openbase_coder_cli.livekit_agent.spoken_commands import (
    _is_exit_to_dispatch_command,
    _normalize_spoken_command,
)
from openbase_coder_cli.livekit_agent.super_agents_client import (
    _looks_like_raw_backend_error,
    _speech_text_from_progress,
)
from openbase_coder_cli.livekit_agent.super_agents_speech import (
    _progress_has_pending_requests,
)
from openbase_coder_cli.onboarding_reminder import append_onboarding_reminder
from openbase_coder_cli.voice_tags import wrap_voice_prompt

logger = logging.getLogger(__name__)

DISPATCH_TIMING_LOG = "dispatch_timing"

# OpenAI caps every ``session.*.append`` at 500 tokens. There is no tokenizer
# here, so chunks are sized with a conservative characters-per-token estimate.
COMMENTARY_MAX_TOKENS = 500
CHARS_PER_TOKEN_ESTIMATE = 3.2
# Finalized utterances the model handled itself are still context for the next
# delegation, but only for so long: stale small talk must not be replayed.
TRANSCRIPT_BUFFER_MAX_AGE_SECONDS = 120.0
TRANSCRIPT_BUFFER_MAX_ITEMS = 8
# Quiet progress for the model while a turn runs, so it can reassure the
# caller instead of guessing.
PROGRESS_THINKING_INTERVAL_SECONDS = 20.0
RESTATEMENT_MAX_CHARS = 120

DISPATCHER_AGENT_LABEL = "the dispatcher"
LIVE_EMPTY_ANSWER_COMMENTARY = "Done, nothing else to report."
LIVE_BACKEND_BUSY_COMMENTARY = (
    "The coding agent is deep in a long task right now and could not take "
    "that; it will catch up as soon as it frees up."
)
LIVE_BACKEND_UNRESPONSIVE_COMMENTARY = (
    "The coding backend is not responding right now, so that request could "
    "not be handled. Ask again in a moment."
)
LIVE_APPROVAL_PENDING_COMMENTARY = (
    "The agent is waiting for an approval before it can continue."
)
LIVE_APPROVAL_PENDING_INSTRUCTIONS = (
    "The agent's turn is paused on an approval request. If the caller asks, "
    "tell them they can approve it from the Approvals tab in the Openbase "
    "phone or desktop app; do not claim the work finished."
)
BACK_TO_DISPATCH_COMMENTARY = "Back to dispatch."

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN_ESTIMATE) if text else 0


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def split_sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE_BOUNDARY_RE.split(text.strip()) if part]


def chunk_commentary(
    text: str,
    *,
    max_tokens: int = COMMENTARY_MAX_TOKENS,
    speech_format: bool = True,
) -> list[str]:
    """Sentence-bounded chunks of at most ``max_tokens`` (estimated).

    With ``speech_format`` (spoken commentary) the text first goes through the
    same ``speech_formatter`` rules as the pipeline's TTS text, so code, paths
    and markdown are spoken the same way on both engines. Thinking and
    instructions are the bridge's own plain prose and are chunked verbatim. A
    single sentence longer than the cap is split at word boundaries.
    """
    max_chars = max(1, int(max_tokens * CHARS_PER_TOKEN_ESTIMATE))
    sentences: list[str] = []
    segments = (
        format_for_speech_segments(text) if speech_format else [" ".join(text.split())]
    )
    for segment in segments:
        for sentence in split_sentences(segment):
            if len(sentence) <= max_chars:
                sentences.append(sentence)
                continue
            words = sentence.split()
            current = ""
            for word in words:
                candidate = f"{current} {word}".strip()
                if current and len(candidate) > max_chars:
                    sentences.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                sentences.append(current)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


@dataclass
class _BufferedUtterance:
    text: str
    at: float


class LiveTranscriptBuffer:
    """Input transcript ring buffer: what the thread has not seen yet.

    GPT-Live's ``delegation_created`` carries no task text and the plugin only
    attaches the caller's *open* utterance. Utterances that already ended
    (the model answered them itself, or the caller paused before the model
    delegated) are kept here until the next delegation consumes them.
    """

    def __init__(
        self,
        *,
        max_age_seconds: float = TRANSCRIPT_BUFFER_MAX_AGE_SECONDS,
        max_items: int = TRANSCRIPT_BUFFER_MAX_ITEMS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_age_seconds = max_age_seconds
        self._items: deque[_BufferedUtterance] = deque(maxlen=max_items)
        self._clock = clock
        self._consumed_pending = ""

    def note_final(self, transcript: str) -> None:
        text = " ".join(transcript.split())
        if not text:
            return
        consumed = self._consumed_pending
        self._consumed_pending = ""
        if consumed:
            folded, consumed_folded = text.casefold(), consumed.casefold()
            if folded == consumed_folded:
                return
            if folded.startswith(consumed_folded):
                text = text[len(consumed) :].strip(" ,.;")
                if not text:
                    return
        self._prune()
        self._items.append(_BufferedUtterance(text=text, at=self._clock()))

    def take_prompt(self, pending_transcript: str) -> str:
        """The prompt for a delegation: unseen finals plus the open utterance."""
        self._prune()
        pending = " ".join((pending_transcript or "").split())
        parts = [item.text for item in self._items]
        self._items.clear()
        if pending:
            pending_folded = pending.casefold()
            parts = [
                part for part in parts if not pending_folded.startswith(part.casefold())
            ]
            parts.append(pending)
            self._consumed_pending = pending
        else:
            self._consumed_pending = ""
        return " ".join(parts).strip()

    def clear(self) -> None:
        self._items.clear()
        self._consumed_pending = ""

    def _prune(self) -> None:
        cutoff = self._clock() - self._max_age_seconds
        while self._items and self._items[0].at < cutoff:
            self._items.popleft()


class LiveSpeechCursor:
    """What has already been spoken for one backend turn.

    Progress snapshots repeat the growing final-answer text; the cursor
    returns only sentences not spoken yet (holding back an unfinished trailing
    sentence until ``final``) and never the same sentence twice, mirroring the
    pipeline's duplicate-suppression semantics for a single turn.
    """

    def __init__(self, *, max_tokens: int = COMMENTARY_MAX_TOKENS) -> None:
        self._max_tokens = max_tokens
        self._spoken_hashes: set[str] = set()

    @property
    def spoke_anything(self) -> bool:
        return bool(self._spoken_hashes)

    def advance(self, text: str, *, final: bool) -> list[str]:
        candidate = (text or "").strip()
        if not candidate:
            return []
        sentences = split_sentences(candidate)
        if not final and sentences and not candidate.endswith((".", "!", "?")):
            # An unfinished trailing sentence waits for the next snapshot or
            # the final answer, so it is never spoken in two halves.
            sentences = sentences[:-1]
        fresh: list[str] = []
        for sentence in sentences:
            sentence_hash = _hash(_normalize_spoken_command(sentence))
            if not sentence_hash or sentence_hash in self._spoken_hashes:
                continue
            self._spoken_hashes.add(sentence_hash)
            fresh.append(sentence)
        if not fresh:
            return []
        return chunk_commentary(" ".join(fresh), max_tokens=self._max_tokens)


@dataclass
class LiveDelegationEntry:
    delegation_id: str
    prompt: str
    route: Any
    client: Any
    record: Any = None
    turn_id: str | None = None
    superseded: bool = False
    approval_notified: bool = False
    created_at: float = field(default_factory=time.monotonic)
    task: asyncio.Task[None] | None = None
    heartbeat: asyncio.Task[None] | None = None


class LiveDelegationBridge:
    """Answer GPT-Live client delegations with Super Agent turns."""

    def __init__(
        self,
        *,
        voice_router,
        delivery_ledger=None,
        developer_instructions: Callable[[], str] = (
            load_direct_livekit_developer_instructions
        ),
        max_commentary_tokens: int = COMMENTARY_MAX_TOKENS,
        progress_thinking_interval: float = PROGRESS_THINKING_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._voice_router = voice_router
        self._ledger = delivery_ledger
        self._developer_instructions = developer_instructions
        self._max_tokens = max_commentary_tokens
        self._heartbeat_interval = progress_thinking_interval
        self._clock = clock
        self._live_session: Any = None
        self.transcript_buffer = LiveTranscriptBuffer(clock=clock)
        self._entries: dict[str, LiveDelegationEntry] = {}
        self._cursors: dict[str, LiveSpeechCursor] = {}
        self._listening_clients: list[Any] = []
        self._handled_command_hashes: deque[str] = deque(maxlen=8)
        self._speaking_record: Any = None
        self._active_agent_label = DISPATCHER_AGENT_LABEL
        self._closed = False

    # wiring

    @property
    def active_agent_label(self) -> str:
        return self._active_agent_label

    def attach(self, live_session) -> None:
        """Subscribe to the plugin session's ``delegation_created`` events."""
        self._live_session = live_session
        live_session.on("delegation_created", self.on_delegation_created)

    def detach(self) -> None:
        if self._live_session is not None:
            try:
                self._live_session.off("delegation_created", self.on_delegation_created)
            except Exception:
                logger.debug("live session off() failed", exc_info=True)
        self._live_session = None

    async def aclose(self) -> None:
        self._closed = True
        self.detach()
        for entry in list(self._entries.values()):
            if entry.heartbeat is not None:
                entry.heartbeat.cancel()
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
        for client in self._listening_clients:
            remove = getattr(client, "remove_turn_progress_listener", None)
            if callable(remove):
                remove(self._on_turn_progress)
        self._listening_clients.clear()

    # model-facing appends

    def _append_thinking(self, text: str, delegation_id: str | None) -> None:
        self._append("append_thinking", text, delegation_id)

    def _append_commentary(self, text: str, delegation_id: str | None) -> None:
        self._append("append_commentary", text, delegation_id)

    def _append_instructions(self, text: str, delegation_id: str | None) -> None:
        self._append("append_instructions", text, delegation_id)

    def _append(self, method: str, text: str, delegation_id: str | None) -> None:
        if self._live_session is None or not text:
            return
        chunks = chunk_commentary(
            text,
            max_tokens=self._max_tokens,
            speech_format=method == "append_commentary",
        )
        for chunk in chunks or [text]:
            try:
                getattr(self._live_session, method)(chunk, delegation_id=delegation_id)
            except Exception:
                logger.warning(
                    "%s stage=live_append_failed method=%s delegation_id=%s",
                    DISPATCH_TIMING_LOG,
                    method,
                    delegation_id or "",
                    exc_info=True,
                )
                return
            logger.info(
                "%s stage=live_%s delegation_id=%s text_len=%d text_hash=%s",
                DISPATCH_TIMING_LOG,
                method,
                delegation_id or "",
                len(chunk),
                _hash(chunk),
            )

    # inputs from the session

    def on_user_transcript(self, transcript: str, *, is_final: bool) -> None:
        text = (transcript or "").strip()
        if not text or not is_final:
            return
        if _is_exit_to_dispatch_command(text):
            command_hash = _hash(_normalize_spoken_command(text))
            if not self._voice_router.is_dispatcher_active:
                self._handled_command_hashes.append(command_hash)
                self._exit_to_dispatch(delegation_id=None)
            return
        self.transcript_buffer.note_final(text)

    def on_delegation_created(self, delegation) -> None:
        if self._closed:
            return
        delegation_id = str(getattr(delegation, "id", "") or "")
        if not delegation_id:
            return
        pending = str(getattr(delegation, "pending_transcript", "") or "")
        prompt = self.transcript_buffer.take_prompt(pending)
        logger.info(
            "%s stage=live_delegation_created delegation_id=%s pending_len=%d "
            "prompt_len=%d prompt_hash=%s active_thread_id=%s",
            DISPATCH_TIMING_LOG,
            delegation_id,
            len(pending),
            len(prompt),
            _hash(prompt) if prompt else "",
            getattr(self._voice_router.active_client, "_thread_id", "") or "",
        )
        if not prompt:
            self._append_thinking(
                "The caller has not said anything new since the last request; "
                "answer from the conversation yourself.",
                delegation_id,
            )
            return
        if _is_exit_to_dispatch_command(prompt):
            command_hash = _hash(_normalize_spoken_command(prompt))
            if command_hash in self._handled_command_hashes:
                self._append_commentary(BACK_TO_DISPATCH_COMMENTARY, delegation_id)
                return
            if not self._voice_router.is_dispatcher_active:
                self._exit_to_dispatch(delegation_id=delegation_id)
                return
            # Dispatcher already active: a normal prompt, as in the pipeline.
        client = self._voice_router.active_client
        route = self._voice_router.route_snapshot()
        for other in self._entries.values():
            if other.client is client and not other.superseded:
                other.superseded = True
                logger.info(
                    "%s stage=live_delegation_superseded delegation_id=%s by=%s",
                    DISPATCH_TIMING_LOG,
                    other.delegation_id,
                    delegation_id,
                )
        entry = LiveDelegationEntry(
            delegation_id=delegation_id,
            prompt=prompt,
            route=route,
            client=client,
            created_at=self._clock(),
        )
        self._entries[delegation_id] = entry
        if self._ledger is not None:
            entry.record = self._ledger.accept_utterance(
                message_id=f"live-{delegation_id}", prompt=prompt
            )
        self._append_thinking(
            f"Request taken by {self._active_agent_label}: "
            f'"{_restatement(prompt)}". Acknowledge briefly in your own words '
            "and keep the conversation going; results arrive as commentary. "
            "Do not invent an outcome.",
            delegation_id,
        )
        self._ensure_progress_listener(client)
        entry.task = asyncio.create_task(
            self._run_delegation(entry),
            name=f"openbase-live-delegation-{delegation_id}",
        )

    def on_agent_state_changed(self, old_state: str, new_state: str) -> None:
        """Synthetic ``agent_audio_started`` / ``agent_audio_finished``."""
        if self._ledger is None:
            return
        if new_state == "speaking" and old_state != "speaking":
            record = self._ledger.live_record_awaiting_audio()
            if record is None:
                record = self._ledger.track_live_speech()
            self._speaking_record = record
            self._ledger.mark_live_audio_started(record)
        elif old_state == "speaking" and new_state != "speaking":
            record, self._speaking_record = self._speaking_record, None
            if record is not None:
                self._ledger.mark_live_audio_finished(record)

    def announce(self, text: str, *, agent_name: str | None = None) -> None:
        """A ``user say`` announcement, woven into the conversation."""
        message = text.strip()
        if not message:
            return
        if agent_name:
            message = f"{agent_name}: {message}"
        self._append_commentary(message, None)

    def notify_route_changed(self, *, action: str, agent_label: str | None) -> None:
        """Tell the one voice who is on the call now (one voice per call)."""
        label = (agent_label or "").strip() or DISPATCHER_AGENT_LABEL
        if action == "exit_to_dispatch":
            label = DISPATCHER_AGENT_LABEL
        self._active_agent_label = label
        self.transcript_buffer.clear()
        instructions = ""
        try:
            instructions = self._developer_instructions() or ""
        except Exception:
            logger.debug("live route instructions unavailable", exc_info=True)
        summary = " ".join(instructions.split())[:400]
        self._append_thinking(
            f"The call is now routed to {label}. Everything you delegate goes "
            f"to {label}; refer to it by that name. "
            + (f"Guidance for that route: {summary}" if summary else ""),
            None,
        )
        if action == "exit_to_dispatch":
            self._append_commentary(BACK_TO_DISPATCH_COMMENTARY, None)
        else:
            self._append_commentary(f"You are now talking to {label}.", None)

    def deliver_orphaned_result(self, client, turn_id: str, speech_text: str) -> None:
        """Speak a completed turn answer no delegation consumed."""
        if not speech_text or not turn_id:
            return
        if not self._voice_router.claim_speech(client, turn_id):
            return
        cursor = self._cursor(turn_id)
        chunks = cursor.advance(speech_text, final=True)
        delegation_id = self._newest_delegation_id_for(client)
        for chunk in chunks:
            self._append_commentary(chunk, delegation_id)

    # delegation execution

    async def _run_delegation(self, entry: LiveDelegationEntry) -> None:
        prompt = wrap_voice_prompt(entry.prompt)
        if self._voice_router.is_dispatcher_active:
            prompt = append_onboarding_reminder(prompt)
        entry.heartbeat = asyncio.create_task(
            self._progress_heartbeat(entry),
            name=f"openbase-live-heartbeat-{entry.delegation_id}",
        )
        try:
            result = await entry.client.run_turn(
                prompt, developer_instructions=self._developer_instructions()
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._on_turn_failed(entry)
            return
        finally:
            if entry.heartbeat is not None:
                entry.heartbeat.cancel()
        self._on_turn_result(entry, result)

    async def _progress_heartbeat(self, entry: LiveDelegationEntry) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            if entry.superseded or self._closed:
                return
            self._append_thinking(
                f'Still working on "{_restatement(entry.prompt)}" with '
                f"{self._active_agent_label}; nothing new to report yet.",
                entry.delegation_id,
            )

    def _on_turn_failed(self, entry: LiveDelegationEntry) -> None:
        busy = False
        appears_busy = getattr(entry.client, "backend_appears_busy", None)
        if callable(appears_busy):
            try:
                busy = bool(appears_busy())
            except Exception:
                busy = False
        logger.exception(
            "%s stage=live_delegation_turn_failed delegation_id=%s backend_busy=%s",
            DISPATCH_TIMING_LOG,
            entry.delegation_id,
            busy,
        )
        if self._ledger is not None and entry.record is not None:
            self._ledger.mark_cancelled(entry.record, reason="live_turn_failed")
        if entry.superseded:
            return
        self._append_commentary(
            LIVE_BACKEND_BUSY_COMMENTARY
            if busy
            else LIVE_BACKEND_UNRESPONSIVE_COMMENTARY,
            entry.delegation_id,
        )

    def _on_turn_result(self, entry: LiveDelegationEntry, result: dict) -> None:
        turn_id = str(result.get("_livekit_turn_id") or "")
        speech_text = str(result.get("_livekit_speech_text") or "")
        entry.turn_id = turn_id or None
        ledger = self._ledger
        if ledger is not None and entry.record is not None and turn_id:
            ledger.mark_answer_owed(entry.record, turn_id=turn_id, client=entry.client)
        if entry.superseded:
            logger.info(
                "%s stage=live_delegation_result_dropped delegation_id=%s "
                "turn_id=%s reason=superseded",
                DISPATCH_TIMING_LOG,
                entry.delegation_id,
                turn_id,
            )
            if ledger is not None and entry.record is not None:
                ledger.mark_cancelled(
                    entry.record, reason="superseded_by_newer_delegation"
                )
            return
        if not self._voice_router.can_deliver_for_snapshot(entry.route):
            logger.info(
                "%s stage=live_delegation_result_dropped delegation_id=%s "
                "turn_id=%s reason=route_changed",
                DISPATCH_TIMING_LOG,
                entry.delegation_id,
                turn_id,
            )
            if ledger is not None and entry.record is not None:
                ledger.mark_suppressed_stale(
                    entry.record, reason="route_changed_before_commentary"
                )
            return
        cursor = self._cursor(turn_id or entry.delegation_id)
        chunks = cursor.advance(speech_text, final=True) if speech_text else []
        if ledger is not None and entry.record is not None and speech_text:
            ledger.mark_text_generated(
                entry.record, speech_text=speech_text, tts_text=speech_text
            )
        if chunks:
            for chunk in chunks:
                self._append_commentary(chunk, entry.delegation_id)
            if turn_id:
                entry.client.claim_speech(turn_id)
            return
        if cursor.spoke_anything:
            # Streamed progress already covered the answer, or a duplicate
            # utterance joined an already-spoken turn: nothing new to say.
            self._append_thinking(
                "That request was already answered; nothing new to add.",
                entry.delegation_id,
            )
            if ledger is not None and entry.record is not None:
                ledger.mark_cancelled(entry.record, reason="live_answer_already_spoken")
            return
        self._append_commentary(LIVE_EMPTY_ANSWER_COMMENTARY, entry.delegation_id)
        if ledger is not None and entry.record is not None and not speech_text:
            ledger.mark_text_generated(
                entry.record,
                speech_text=LIVE_EMPTY_ANSWER_COMMENTARY,
                tts_text=LIVE_EMPTY_ANSWER_COMMENTARY,
            )

    # progress streaming

    def _ensure_progress_listener(self, client) -> None:
        if client in self._listening_clients:
            return
        add = getattr(client, "add_turn_progress_listener", None)
        if not callable(add):
            return
        add(self._on_turn_progress)
        self._listening_clients.append(client)

    def _on_turn_progress(self, client, turn_id: str, progress: dict) -> None:
        entry = self._newest_entry_for(client)
        if entry is None:
            return
        if entry.turn_id is None:
            entry.turn_id = turn_id
        if not self._voice_router.can_deliver_for_snapshot(entry.route):
            return
        if _progress_has_pending_requests(progress) and not entry.approval_notified:
            entry.approval_notified = True
            self._append_commentary(
                LIVE_APPROVAL_PENDING_COMMENTARY, entry.delegation_id
            )
            self._append_instructions(
                LIVE_APPROVAL_PENDING_INSTRUCTIONS, entry.delegation_id
            )
        text = _speech_text_from_progress(progress)
        if not text or _looks_like_raw_backend_error(text):
            return
        for chunk in self._cursor(turn_id).advance(text, final=False):
            self._append_commentary(chunk, entry.delegation_id)

    # helpers

    def _exit_to_dispatch(self, *, delegation_id: str | None) -> None:
        changed = self._voice_router.exit_to_dispatch()
        for entry in self._entries.values():
            if not entry.superseded:
                entry.superseded = True
        self._active_agent_label = DISPATCHER_AGENT_LABEL
        self.transcript_buffer.clear()
        if changed or delegation_id is not None:
            self._append_commentary(BACK_TO_DISPATCH_COMMENTARY, delegation_id)

    def _cursor(self, key: str) -> LiveSpeechCursor:
        cursor = self._cursors.get(key)
        if cursor is None:
            cursor = self._cursors[key] = LiveSpeechCursor(max_tokens=self._max_tokens)
            while len(self._cursors) > 32:
                self._cursors.pop(next(iter(self._cursors)))
        return cursor

    def _newest_entry_for(self, client) -> LiveDelegationEntry | None:
        newest: LiveDelegationEntry | None = None
        for entry in self._entries.values():
            if entry.client is client and not entry.superseded:
                if newest is None or entry.created_at >= newest.created_at:
                    newest = entry
        return newest

    def _newest_delegation_id_for(self, client) -> str | None:
        entry = self._newest_entry_for(client)
        return entry.delegation_id if entry is not None else None


def _restatement(prompt: str) -> str:
    text = " ".join(prompt.split())
    if len(text) <= RESTATEMENT_MAX_CHARS:
        return text
    return text[: RESTATEMENT_MAX_CHARS - 3].rsplit(" ", 1)[0].strip() + "..."
