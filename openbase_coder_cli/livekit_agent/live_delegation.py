"""GPT-Live voice bridge to Super Agent threads: the agent is always the brain.

Under the Live Voice engine the voice model (``GPTLiveModel``,
``delegation="client"``) is the only speaker but never the brain. Every
substantive caller utterance goes to the active Super Agent thread
(dispatcher, direct thread, or a transferred thread) exactly as every STT
final did on the pipeline engine, so the thread's MCP servers, skills and
filesystem answer the caller, not the voice model's general knowledge.

:class:`LiveDelegationBridge` starts a thread turn (or joins the running one
through ``run_turn``'s steering) when the plugin closes a caller utterance,
whether or not GPT-Live also emits ``delegation_created``. The utterance
signal is the plugin's final ``input_audio_transcription_completed`` event,
emitted from ``GPTLiveSession._end_speech`` once 0.8 s of caller audio has
passed with no new transcript fragment (or the next fragment starts that long
after the previous one); it is the only per-utterance boundary the plugin
exposes, it carries the whole utterance text and a stable item id, and it is
exactly what ``AgentSession`` re-emits as a final ``user_input_transcribed``.
That boundary is timed on audio, not on the transcript, so a closed
utterance is first held for ``UTTERANCE_SETTLE_SECONDS`` and merged with a
fragment that closes (or opens) meanwhile; a delegation flushes the hold at
once. A delegation the model does emit is bound to the turn already running
for the same utterance instead of starting a second one, and spoken results
then go out as ``append_commentary`` bound to that delegation; without one
they go out with ``delegation_id=None``. Progress-only output is ``append_thinking``.
Commentary is sentence-bounded, in chunks of at most 500 tokens. Pure small
talk and noise (``is_trivial_utterance``) is the one thing kept off the agent.
See ``dev-docs/live-voice.md``.

The bridge also keeps the voice lifecycle contract alive without a TTS path:
``utterance_accepted`` when a turn is accepted, ``agent_audio_started`` /
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
# Quiet progress for the model while a turn runs, so it can reassure the
# caller instead of guessing.
PROGRESS_THINKING_INTERVAL_SECONDS = 20.0
RESTATEMENT_MAX_CHARS = 120
# A GPT-Live delegation that arrives after the caller's utterance closed binds
# to the turn the bridge already started for it when that turn is this recent.
DELEGATION_BIND_WINDOW_SECONDS = 15.0
# A trivial utterance the bridge kept off the agent is still sent when the
# model delegates right after it: the model judged it substantive.
SKIPPED_UTTERANCE_MAX_AGE_SECONDS = 15.0
# A spoken command handled off the transcript answers the model's delegation
# of the same words (and vice versa) for this long.
COMMAND_DEDUPE_WINDOW_SECONDS = 10.0
# Entries kept for ownership decisions; older completed ones are dropped.
MAX_TRACKED_ENTRIES = 32
# The plugin closes a caller utterance after 0.8 s of audio with no new
# transcript fragment (``GPTLiveSession.push_audio``), timed on audio frames,
# not on the transcript. A sentence-final word whose transcript lags behind
# the audio therefore closes as a second utterance ("What files are on my",
# then "desktop" about a second later), and a pause mid-sentence does the
# same. A closed utterance is held this long for a continuation before it goes
# to the agent; a fragment that opens during the hold extends it (bounded by
# the max), and a delegation from the model flushes it at once, so a request
# the model acts on loses no time.
UTTERANCE_SETTLE_SECONDS = 0.7
UTTERANCE_HOLD_MAX_SECONDS = 3.0

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


# Utterances that never need the agent: noise and filler fragments, greetings,
# thanks, farewells and pure acknowledgements, written in the normalized form
# of ``_normalize_spoken_command`` (lowercase, punctuation as spaces). Answers
# the agent may be waiting for ("yes", "no", "sure", "go ahead", "do it") and
# commands ("stop", "wait", "cancel") are deliberately absent. A skipped
# utterance still reaches the agent when the model delegates it.
TRIVIAL_UTTERANCE_PHRASES = frozenset(
    {
        # fillers, backchannels and fragments cut off by a pause
        "ah",
        "and",
        "eh",
        "er",
        "erm",
        "hm",
        "hmm",
        "hmmm",
        "huh",
        "mhm",
        "mm",
        "mm hmm",
        "mmhmm",
        "mmm",
        "oh",
        "ooh",
        "so",
        "uh",
        "uh huh",
        "uhh",
        "uhm",
        "um",
        "umm",
        "well",
        # greetings
        "good afternoon",
        "good evening",
        "good morning",
        "hello",
        "hello there",
        "hey",
        "hey there",
        "hi",
        "hi there",
        "hiya",
        "morning",
        "yo",
        # thanks
        "appreciate it",
        "cheers",
        "much appreciated",
        "thank you",
        "thank you so much",
        "thanks",
        "thanks a lot",
        "thanks so much",
        "thx",
        # acknowledgements
        "all right",
        "alright",
        "awesome",
        "cool",
        "got it",
        "great",
        "i see",
        "k",
        "nice",
        "ok",
        "okay",
        "perfect",
        "right",
        "sounds good",
        # farewells
        "bye",
        "bye bye",
        "goodbye",
        "good night",
        "see you",
        "see ya",
    }
)
TRIVIAL_UTTERANCE_MAX_WORDS = 6
_TRIVIAL_PHRASE_MAX_WORDS = max(len(p.split()) for p in TRIVIAL_UTTERANCE_PHRASES)


def is_trivial_utterance(text: str) -> bool:
    """True only for an utterance made entirely of known trivial phrases.

    Conservative by construction, so it errs toward the agent: a single word
    outside ``TRIVIAL_UTTERANCE_PHRASES``, or more than
    ``TRIVIAL_UTTERANCE_MAX_WORDS`` words, sends the utterance to the agent.
    Empty or punctuation-only fragments (noise) are trivial.
    """
    words = _normalize_spoken_command(text or "").split()
    if not words:
        return True
    if len(words) > TRIVIAL_UTTERANCE_MAX_WORDS:
        return False
    index = 0
    while index < len(words):
        for size in range(min(_TRIVIAL_PHRASE_MAX_WORDS, len(words) - index), 0, -1):
            if " ".join(words[index : index + size]) in TRIVIAL_UTTERANCE_PHRASES:
                index += size
                break
        else:
            return False
    return True


def remainder_after(prefix: str, text: str) -> str | None:
    """``text`` minus a leading ``prefix``, compared as normalized words.

    Returns the rest of ``text`` in its original wording ("" when nothing
    follows), or ``None`` when ``text`` does not start with ``prefix`` (the
    transcript was revised, or it is another utterance).
    """
    prefix_words = _normalize_spoken_command(prefix or "").split()
    tokens = (text or "").split()
    if not prefix_words:
        return " ".join(tokens)
    consumed: list[str] = []
    index = 0
    while index < len(tokens) and len(consumed) < len(prefix_words):
        consumed.extend(_normalize_spoken_command(tokens[index]).split())
        index += 1
    if consumed[: len(prefix_words)] != prefix_words:
        return None
    return " ".join(tokens[index:]).strip(" ,.;")


@dataclass
class _SkippedUtterance:
    text: str
    at: float


@dataclass
class _HeldUtterance:
    """A closed caller utterance waiting briefly for its continuation."""

    text: str
    first_at: float
    item_ids: list[str] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None


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
    """One caller utterance handed to a Super Agent turn.

    ``key`` names the entry: the delegation id when GPT-Live's delegation
    started it, otherwise ``utt-<n>``. ``delegation_id`` is the GPT-Live
    delegation its spoken results answer; ``None`` until one binds, in which
    case commentary goes out session-wide. ``open_utterance`` is the caller's
    still-open utterance text a delegation started the turn on, kept until
    that utterance's final transcript arrives so the final is not sent twice.
    """

    key: str
    prompt: str
    route: Any
    client: Any
    delegation_id: str | None = None
    source: str = "transcript"
    open_utterance: str = ""
    # Held fragments that preceded ``open_utterance`` in the same request, so
    # a steer with the utterance's final words keeps the whole request.
    lead_in: str = ""
    record: Any = None
    turn_id: str | None = None
    superseded: bool = False
    completed: bool = False
    approval_notified: bool = False
    created_at: float = field(default_factory=time.monotonic)
    task: asyncio.Task[None] | None = None
    heartbeat: asyncio.Task[None] | None = None


class LiveDelegationBridge:
    """Send every caller utterance to the active Super Agent; voice its answers."""

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
        utterance_settle_seconds: float = UTTERANCE_SETTLE_SECONDS,
        utterance_hold_max_seconds: float = UTTERANCE_HOLD_MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._voice_router = voice_router
        self._ledger = delivery_ledger
        self._developer_instructions = developer_instructions
        self._max_tokens = max_commentary_tokens
        self._heartbeat_interval = progress_thinking_interval
        self._settle_seconds = utterance_settle_seconds
        self._hold_max_seconds = utterance_hold_max_seconds
        self._clock = clock
        self._held: _HeldUtterance | None = None
        self._live_session: Any = None
        self._entries: dict[str, LiveDelegationEntry] = {}
        self._cursors: dict[str, LiveSpeechCursor] = {}
        self._listening_clients: list[Any] = []
        self._handled_item_ids: deque[str] = deque(maxlen=64)
        self._last_exit_command_at: float | None = None
        self._skipped: _SkippedUtterance | None = None
        self._utterance_seq = 0
        self._speaking_record: Any = None
        self._active_agent_label = DISPATCHER_AGENT_LABEL
        self._closed = False

    # wiring

    @property
    def active_agent_label(self) -> str:
        return self._active_agent_label

    def _session_handlers(self) -> tuple[tuple[str, Callable[..., None]], ...]:
        return (
            ("input_audio_transcription_completed", self._on_input_transcription),
            ("delegation_created", self.on_delegation_created),
        )

    def attach(self, live_session) -> None:
        """Subscribe to the plugin session: closed caller utterances and delegations."""
        self._live_session = live_session
        for event_name, handler in self._session_handlers():
            live_session.on(event_name, handler)

    def detach(self) -> None:
        if self._live_session is not None:
            for event_name, handler in self._session_handlers():
                try:
                    self._live_session.off(event_name, handler)
                except Exception:
                    logger.debug("live session off() failed", exc_info=True)
        self._live_session = None

    async def aclose(self) -> None:
        self._closed = True
        self.detach()
        self._take_held()
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

    def _on_input_transcription(self, event) -> None:
        self.on_user_transcript(
            str(getattr(event, "transcript", "") or ""),
            is_final=bool(getattr(event, "is_final", False)),
            item_id=str(getattr(event, "item_id", "") or "") or None,
        )

    def on_user_transcript(
        self, transcript: str, *, is_final: bool, item_id: str | None = None
    ) -> None:
        """A caller utterance closed: send it to the agent unless already sent.

        Called for the plugin's final ``input_audio_transcription_completed``
        (see the module docstring for why that is the utterance boundary).
        Partial transcripts are ignored. The utterance is covered, and not
        sent again, when a delegation already started a turn on its open text
        and the caller added nothing substantive after it; otherwise the whole
        final utterance steers that turn, so the agent reads the caller's
        complete words rather than a dangling tail.
        """
        text = " ".join((transcript or "").split())
        if self._closed or not text:
            return
        if not is_final:
            self._note_open_fragment(item_id)
            return
        if item_id:
            if item_id in self._handled_item_ids:
                return
            self._handled_item_ids.append(item_id)
        if _is_exit_to_dispatch_command(text):
            if self._take_recent_exit_command():
                # The model delegated this command first; it is handled.
                self._log_forced(text, decision="exit_command_already_handled")
                return
            if not self._voice_router.is_dispatcher_active:
                self._last_exit_command_at = self._clock()
                self._log_forced(text, decision="exit_to_dispatch")
                self._exit_to_dispatch(delegation_id=None)
                return
            # Dispatcher already active: a normal prompt, as in the pipeline.
        # Newer speech: a later empty delegation is about it, not the command.
        self._last_exit_command_at = None
        open_entry = self._open_utterance_entry()
        if open_entry is not None:
            remainder = remainder_after(open_entry.open_utterance, text)
            open_entry.open_utterance = ""
            if remainder is not None:
                if not remainder or is_trivial_utterance(remainder):
                    self._log_forced(
                        text,
                        decision="covered_by_delegation",
                        key=open_entry.key,
                        delegation_id=open_entry.delegation_id,
                    )
                    return
            # The caller kept talking after the model delegated, or the final
            # revised the words the turn started on: the full final steers
            # the same turn (and inherits its delegation), with the fragments
            # that led into it.
            if open_entry.lead_in:
                text = f"{open_entry.lead_in} {text}"
            self._dispatch_utterance(text)
            return
        self._hold_utterance(text, item_id)

    def _dispatch_utterance(self, text: str) -> None:
        """A whole caller utterance, settled: to the agent unless trivial."""
        if _is_exit_to_dispatch_command(text):
            if self._take_recent_exit_command():
                self._log_forced(text, decision="exit_command_already_handled")
                return
            if not self._voice_router.is_dispatcher_active:
                self._last_exit_command_at = self._clock()
                self._log_forced(text, decision="exit_to_dispatch")
                self._exit_to_dispatch(delegation_id=None)
                return
        if is_trivial_utterance(text):
            self._skipped = _SkippedUtterance(text=text, at=self._clock())
            self._log_forced(text, decision="skipped_trivial")
            return
        self._skipped = None
        self._start_turn(text, source="transcript")

    # fragment settling

    def _hold_utterance(self, text: str, item_id: str | None) -> None:
        now = self._clock()
        held = self._held
        if held is None:
            held = self._held = _HeldUtterance(text=text, first_at=now)
            self._log_forced(text, decision="held", key="")
        else:
            held.text = f"{held.text} {text}".strip()
            self._log_forced(held.text, decision="merged_fragment", key="")
        if item_id:
            held.item_ids.append(item_id)
        self._schedule_flush(now + self._settle_seconds)

    def _note_open_fragment(self, item_id: str | None) -> None:
        """A new transcript fragment opened while an utterance is held: wait for it."""
        held = self._held
        if held is None or not item_id or item_id in held.item_ids:
            return
        if item_id in self._handled_item_ids:
            return
        self._schedule_flush(held.first_at + self._hold_max_seconds)

    def _schedule_flush(self, at: float) -> None:
        held = self._held
        if held is None:
            return
        deadline = min(at, held.first_at + self._hold_max_seconds)
        if held.timer is not None:
            held.timer.cancel()
        delay = max(0.0, deadline - self._clock())
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush_held()
            return
        held.timer = loop.call_later(delay, self._flush_held)

    def _flush_held(self) -> None:
        held = self._take_held()
        if held is None or self._closed:
            return
        self._log_forced(held.text, decision="settled", key="")
        self._dispatch_utterance(held.text)

    def _take_held(self) -> _HeldUtterance | None:
        held, self._held = self._held, None
        if held is not None and held.timer is not None:
            held.timer.cancel()
            held.timer = None
        return held

    def on_delegation_created(self, delegation) -> None:
        """Bind GPT-Live's delegation to the agent turn for that utterance.

        The bridge already sends every closed utterance to the agent, so a
        delegation never decides *whether* the agent hears the caller. It
        decides which delegation the spoken answer is bound to, and it starts
        the turn early when the model delegates while the caller's utterance
        is still open (``pending_transcript``).
        """
        if self._closed:
            return
        delegation_id = str(getattr(delegation, "id", "") or "")
        if not delegation_id:
            return
        pending = " ".join(
            str(getattr(delegation, "pending_transcript", "") or "").split()
        )
        logger.info(
            "%s stage=live_delegation_created delegation_id=%s pending_len=%d "
            "pending_hash=%s active_thread_id=%s",
            DISPATCH_TIMING_LOG,
            delegation_id,
            len(pending),
            _hash(pending) if pending else "",
            getattr(self._voice_router.active_client, "_thread_id", "") or "",
        )
        held = self._take_held()
        if held is not None:
            self._start_turn_from_held(held, pending, delegation_id)
            return
        if not pending:
            self._on_delegation_after_utterance(delegation_id)
            return
        if _is_exit_to_dispatch_command(pending):
            if self._take_recent_exit_command():
                self._append_commentary(BACK_TO_DISPATCH_COMMENTARY, delegation_id)
                return
            if not self._voice_router.is_dispatcher_active:
                self._last_exit_command_at = self._clock()
                self._exit_to_dispatch(delegation_id=delegation_id)
                return
            # Dispatcher already active: a normal prompt, as in the pipeline.
        self._last_exit_command_at = None
        open_entry = self._open_utterance_entry()
        if open_entry is not None:
            remainder = remainder_after(open_entry.open_utterance, pending)
            if remainder is not None:
                # The model delegated the same open utterance again.
                if not remainder or is_trivial_utterance(remainder):
                    open_entry.open_utterance = pending
                    self._bind(open_entry, delegation_id)
                    return
                open_entry.open_utterance = ""
                self._start_turn(
                    pending,
                    source="delegation",
                    delegation_id=delegation_id,
                    key=delegation_id,
                    open_utterance=pending,
                )
                return
            open_entry.open_utterance = ""
        if is_trivial_utterance(pending):
            unbound = self._unbound_recent_entry()
            if unbound is not None:
                # Small talk opened while the last request's turn runs: the
                # delegation is about that request.
                self._bind(unbound, delegation_id)
                return
        self._start_turn(
            pending,
            source="delegation",
            delegation_id=delegation_id,
            key=delegation_id,
            open_utterance=pending,
        )

    def _start_turn_from_held(
        self, held: _HeldUtterance, pending: str, delegation_id: str
    ) -> None:
        """The model delegated while fragments were held: one request, now."""
        if not pending:
            text, open_utterance, lead_in = held.text, "", ""
        elif remainder_after(held.text, pending) is not None:
            # The model's open turn already contains the held words.
            text, open_utterance, lead_in = pending, pending, ""
        else:
            text = f"{held.text} {pending}"
            open_utterance, lead_in = pending, held.text
        self._log_forced(text, decision="settled", key="", delegation_id=delegation_id)
        if _is_exit_to_dispatch_command(text):
            if self._take_recent_exit_command():
                self._append_commentary(BACK_TO_DISPATCH_COMMENTARY, delegation_id)
                return
            if not self._voice_router.is_dispatcher_active:
                self._last_exit_command_at = self._clock()
                self._exit_to_dispatch(delegation_id=delegation_id)
                return
        self._last_exit_command_at = None
        self._skipped = None
        entry = self._start_turn(
            text,
            source="delegation",
            delegation_id=delegation_id,
            key=delegation_id,
            open_utterance=open_utterance,
        )
        entry.lead_in = lead_in

    def _on_delegation_after_utterance(self, delegation_id: str) -> None:
        """A delegation with no open utterance: the caller's words already closed."""
        if self._take_recent_exit_command():
            self._append_commentary(BACK_TO_DISPATCH_COMMENTARY, delegation_id)
            return
        entry = self._unbound_recent_entry()
        if entry is not None:
            self._bind(entry, delegation_id)
            return
        skipped = self._take_skipped()
        if skipped is not None:
            self._start_turn(
                skipped.text,
                source="delegation",
                delegation_id=delegation_id,
                key=delegation_id,
            )
            return
        logger.info(
            "%s stage=live_delegation_without_new_speech delegation_id=%s",
            DISPATCH_TIMING_LOG,
            delegation_id,
        )
        self._append_thinking(
            f"Nothing new from the caller has gone to {self._active_agent_label} "
            "since its last request; its answer arrives as commentary. Do not "
            "answer from your own knowledge. If the caller's words were unclear, "
            "ask them to repeat.",
            delegation_id,
        )

    def _start_turn(
        self,
        text: str,
        *,
        source: str,
        delegation_id: str | None = None,
        key: str | None = None,
        open_utterance: str = "",
    ) -> LiveDelegationEntry:
        """Run (or steer) the active agent's turn on one caller utterance."""
        client = self._voice_router.active_client
        route = self._voice_router.route_snapshot()
        inherited: LiveDelegationEntry | None = None
        for other in self._entries.values():
            if other.client is not client or other.superseded:
                continue
            other.superseded = True
            # run_turn steers the running turn, so its merged answer also
            # answers the delegation the superseded utterance was bound to.
            if (
                not other.completed
                and other.delegation_id
                and (inherited is None or other.created_at >= inherited.created_at)
            ):
                inherited = other
            logger.info(
                "%s stage=live_delegation_superseded key=%s delegation_id=%s",
                DISPATCH_TIMING_LOG,
                other.key,
                other.delegation_id or "",
            )
        if key is None:
            self._utterance_seq += 1
            key = f"utt-{self._utterance_seq}"
        entry = LiveDelegationEntry(
            key=key,
            prompt=text,
            route=route,
            client=client,
            delegation_id=delegation_id
            or (inherited.delegation_id if inherited is not None else None),
            source=source,
            open_utterance=open_utterance,
            created_at=self._clock(),
        )
        self._entries[key] = entry
        self._prune_entries()
        self._log_forced(
            text,
            decision="started",
            key=key,
            delegation_id=entry.delegation_id,
            source=source,
        )
        if self._ledger is not None:
            entry.record = self._ledger.accept_utterance(
                message_id=f"live-{key}", prompt=text
            )
        self._append_thinking(self._taken_thinking(entry), entry.delegation_id)
        self._ensure_progress_listener(client)
        entry.task = asyncio.create_task(
            self._run_delegation(entry),
            name=f"openbase-live-turn-{key}",
        )
        return entry

    def _bind(self, entry: LiveDelegationEntry, delegation_id: str) -> None:
        previous = entry.delegation_id
        entry.delegation_id = delegation_id
        logger.info(
            "%s stage=live_delegation_bound key=%s delegation_id=%s previous=%s "
            "completed=%s",
            DISPATCH_TIMING_LOG,
            entry.key,
            delegation_id,
            previous or "",
            entry.completed,
        )
        if entry.completed:
            self._append_thinking(
                f"{self._active_agent_label} already answered that request and "
                "its answer was spoken; do not repeat it or add to it.",
                delegation_id,
            )
            return
        self._append_thinking(self._taken_thinking(entry), delegation_id)

    def _taken_thinking(self, entry: LiveDelegationEntry) -> str:
        restated = _restatement(entry.prompt)
        label = self._active_agent_label
        if entry.delegation_id:
            return (
                f'Request taken by {label}: "{restated}". Acknowledge briefly in '
                "your own words and keep the conversation going; the answer "
                "arrives as commentary. Do not answer it yourself, guess, or "
                "invent an outcome."
            )
        return (
            f'The caller said "{restated}" and it went to {label}, which '
            "answers; its answer arrives as commentary. Acknowledge briefly if "
            "it fits, but do not answer it yourself, guess, or invent an outcome."
        )

    def _log_forced(
        self,
        text: str,
        *,
        decision: str,
        key: str = "",
        delegation_id: str | None = None,
        source: str = "transcript",
    ) -> None:
        logger.info(
            "%s stage=live_forced_delegation decision=%s key=%s source=%s "
            "delegation_id=%s text_len=%d text_hash=%s active_thread_id=%s",
            DISPATCH_TIMING_LOG,
            decision,
            key,
            source,
            delegation_id or "",
            len(text),
            _hash(text) if text else "",
            getattr(self._voice_router.active_client, "_thread_id", "") or "",
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
        self._reset_utterance_state()
        instructions = ""
        try:
            instructions = self._developer_instructions() or ""
        except Exception:
            logger.debug("live route instructions unavailable", exc_info=True)
        summary = " ".join(instructions.split())[:400]
        self._append_thinking(
            f"The call is now routed to {label}. Everything the caller says goes "
            f"to {label}, which answers; refer to it by that name. "
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
            name=f"openbase-live-heartbeat-{entry.key}",
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
        entry.completed = True
        busy = False
        appears_busy = getattr(entry.client, "backend_appears_busy", None)
        if callable(appears_busy):
            try:
                busy = bool(appears_busy())
            except Exception:
                busy = False
        logger.exception(
            "%s stage=live_delegation_turn_failed key=%s delegation_id=%s "
            "backend_busy=%s",
            DISPATCH_TIMING_LOG,
            entry.key,
            entry.delegation_id or "",
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
        entry.completed = True
        turn_id = str(result.get("_livekit_turn_id") or "")
        speech_text = str(result.get("_livekit_speech_text") or "")
        entry.turn_id = turn_id or None
        ledger = self._ledger
        if ledger is not None and entry.record is not None and turn_id:
            ledger.mark_answer_owed(entry.record, turn_id=turn_id, client=entry.client)
        if entry.superseded:
            logger.info(
                "%s stage=live_delegation_result_dropped key=%s delegation_id=%s "
                "turn_id=%s reason=superseded",
                DISPATCH_TIMING_LOG,
                entry.key,
                entry.delegation_id or "",
                turn_id,
            )
            if ledger is not None and entry.record is not None:
                ledger.mark_cancelled(
                    entry.record, reason="superseded_by_newer_delegation"
                )
            return
        if not self._voice_router.can_deliver_for_snapshot(entry.route):
            logger.info(
                "%s stage=live_delegation_result_dropped key=%s delegation_id=%s "
                "turn_id=%s reason=route_changed",
                DISPATCH_TIMING_LOG,
                entry.key,
                entry.delegation_id or "",
                turn_id,
            )
            if ledger is not None and entry.record is not None:
                ledger.mark_suppressed_stale(
                    entry.record, reason="route_changed_before_commentary"
                )
            return
        cursor = self._cursor(turn_id or entry.key)
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
            # Streamed progress already relayed this turn's answer as
            # commentary: add nothing. A thinking note here ("already
            # answered") arrived a moment after that commentary and GPT-Live
            # spoke the note instead of the answer (staging, 2026-10-08).
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
        # Turn-scoped: a running turn's snapshot still carries the PREVIOUS
        # answer at session level, and relaying it as commentary made GPT-Live
        # speak the last question's answer before the new one.
        text = _speech_text_from_progress(progress, turn_scoped=True, turn_id=turn_id)
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
        self._reset_utterance_state()
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

    def _open_utterance_entry(self) -> LiveDelegationEntry | None:
        """The newest turn a delegation started on a still-open utterance."""
        newest: LiveDelegationEntry | None = None
        for entry in self._entries.values():
            if entry.open_utterance and (
                newest is None or entry.created_at >= newest.created_at
            ):
                newest = entry
        return newest

    def _unbound_recent_entry(self) -> LiveDelegationEntry | None:
        """The newest live turn no delegation is bound to yet, if recent."""
        cutoff = self._clock() - DELEGATION_BIND_WINDOW_SECONDS
        newest: LiveDelegationEntry | None = None
        for entry in self._entries.values():
            if entry.superseded or entry.delegation_id or entry.created_at < cutoff:
                continue
            if newest is None or entry.created_at >= newest.created_at:
                newest = entry
        return newest

    def _take_recent_exit_command(self) -> bool:
        at, self._last_exit_command_at = self._last_exit_command_at, None
        return at is not None and self._clock() - at <= COMMAND_DEDUPE_WINDOW_SECONDS

    def _take_skipped(self) -> _SkippedUtterance | None:
        skipped, self._skipped = self._skipped, None
        if skipped is None:
            return None
        if self._clock() - skipped.at > SKIPPED_UTTERANCE_MAX_AGE_SECONDS:
            return None
        return skipped

    def _reset_utterance_state(self) -> None:
        """A route change: open utterances and skipped small talk start over."""
        self._skipped = None
        self._take_held()
        for entry in self._entries.values():
            entry.open_utterance = ""
            entry.lead_in = ""

    def _prune_entries(self) -> None:
        if len(self._entries) <= MAX_TRACKED_ENTRIES:
            return
        for key in list(self._entries):
            if len(self._entries) <= MAX_TRACKED_ENTRIES:
                return
            if self._entries[key].completed:
                del self._entries[key]


def _restatement(prompt: str) -> str:
    text = " ".join(prompt.split())
    if len(text) <= RESTATEMENT_MAX_CHARS:
        return text
    return text[: RESTATEMENT_MAX_CHARS - 3].rsplit(" ", 1)[0].strip() + "..."
