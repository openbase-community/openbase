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
once. When the rest of a request arrives after its first half already started
a turn, the merged request replaces that turn (``run_turn(...,
replaces_active_turn=True)``: a mid-turn steer where the backend supports it,
interrupt-and-rerun where it does not), avoiding a full queued replay.
A delegation the model does emit is bound to the turn already running
for the same utterance instead of starting a second one, and spoken results
then go out as ``append_commentary`` bound to that delegation; without one
they go out with ``delegation_id=None``. Progress-only output is ``append_thinking``.
Commentary is sentence-bounded, in chunks of at most 500 tokens. Pure small
talk and noise (``is_trivial_utterance``) is the one thing kept off the agent.
See ``dev-docs/live-voice.md``.

When the gateway socket drops the plugin opens a new GPT-Live session and
reseeds it from the chat history, but the old session's delegations are gone
and whatever was queued for them is rejected. The bridge watches the plugin's
recoverable connection error (``_on_session_error``) and, from then until
``session_reconnected``, keeps each turn's commentary and final answer on its
entry instead of appending them (``_deliver``); it also keeps every bound
append the model did not start speaking after (``_confirm_deliveries``).
``on_session_reconnected`` briefs the new session and re-appends all of that
session-wide, once, flagged as the answer when the turn is complete.
Held chunks are retained for the whole outage; sent chunks remain eligible
for 30 seconds before the drop. Steering a running turn transfers its unheard
chunks to the newer entry because both entries share the turn's speech cursor.
Speaking events during the outage cannot confirm deliveries. Outside it,
speaking-start confirmation is a timing heuristic, not a per-chunk playback
acknowledgement; live field testing must check for partial losses and repeats.

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
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from openbase_coder_cli.livekit_agent.config import (
    load_direct_livekit_developer_instructions,
)
from openbase_coder_cli.livekit_agent.screen_context import apply_screen_context
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
# the max). A delegation flushes the hold at once while the caller is silent
# (the model judged the request complete); while they are still speaking it
# binds to the held words until they settle.
UTTERANCE_SETTLE_SECONDS = 0.7
UTTERANCE_HOLD_MAX_SECONDS = 6.0
# GPT-Live's transcript trails the caller's audio. A held utterance therefore
# also waits this long after the caller's voice (session VAD) last stopped, so
# a sentence already spoken but not yet transcribed joins it: BUG 18
# (2026-10-09) split "Subtract 38 from the multiplication result in this
# thread." from ". Answer just the number", 3 s apart, into two agent turns.
# A normal one-sentence utterance closes 0.8 s after its last transcript
# fragment, which is already past this point, so it pays nothing extra.
UTTERANCE_TRANSCRIPT_LAG_SECONDS = 1.5
# A fragment that arrives after a turn already started on the caller's
# previous words, before that turn said anything, continues that request when
# it reads as the rest of a sentence: punctuation-led (". Answer just the
# number") or opened by a coordinating conjunction ("and
# answer just the number"). The merged request then *replaces* the fragment
# turn (``LiveDelegationEntry.replaces_turn`` -> ``run_turn(...,
# replaces_active_turn=True)``): a backend that steers mid-turn absorbs the
# rest, and one that cannot (Claude Code queues a steer as a separate turn)
# interrupts the fragment turn first. Lowercase alone is not evidence of a
# continuation: "run the linter" must not replay the previous command.
CONTINUATION_SECONDS = 8.0
_LEADING_PUNCTUATION = ".,;:!?"
_CONTINUATION_LEAD_WORDS = frozenset({"and", "or", "but", "nor"})

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
# The plugin reconnects after the gateway socket drops (an Openbase Cloud
# deploy replaces the relay task mid-call) and reseeds the new GPT-Live
# session from its chat history; the delegations of the dropped session are
# gone, so a result bound to one would answer nothing.
LIVE_RECONNECTED_THINKING = (
    "The voice connection dropped and was re-established; the conversation "
    "so far was restored. Do not greet the caller again or start over. If "
    "the caller was mid-sentence when it dropped, ask them to repeat only "
    "that. {pending}"
)
LIVE_RECONNECTED_PENDING = (
    "{label} is still working on the caller's last request; its answer "
    "arrives as commentary."
)
# Commentary the caller has not heard yet is re-appended after the briefing:
# what the turn produced while the socket was down, and what went out bound
# to the dropped session's delegation without the model speaking after it.
LIVE_RECONNECTED_REDELIVERY = (
    "The connection dropped before the caller heard the following from "
    "{label}. Relay it now; {disposition}. Do not repeat any of it the "
    "conversation shows you already said."
)
LIVE_REDELIVERY_ANSWER = (
    "it is the answer to the caller's last request, so present it as the "
    "answer, not as an update"
)
LIVE_REDELIVERY_PROGRESS = (
    "it is progress on the caller's last request, which {label} is still working on"
)
# A bound append counts as spoken once the model starts speaking this long
# after it; an earlier start was the response already under way.
DELIVERY_CONFIRM_MIN_SECONDS = 0.3
# Commentary older than this at the drop was spoken long ago, whatever the
# speaking state said; the model does not sit on commentary for half a minute.
REDELIVERY_MAX_AGE_SECONDS = 30.0

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


def join_fragments(first: str, second: str) -> str:
    """Two transcript fragments as one utterance.

    A continuation that begins with the sentence's own punctuation
    (". Answer just the number") attaches without a space.
    """
    first = (first or "").strip()
    second = (second or "").strip()
    if not first:
        return second
    if not second:
        return first
    if second[0] in _LEADING_PUNCTUATION:
        return f"{first}{second}"
    return f"{first} {second}"


def looks_like_continuation(text: str) -> bool:
    """Whether ``text`` reads as the rest of a sentence rather than a new one."""
    text = (text or "").strip()
    if not text:
        return False
    if text[0] in _LEADING_PUNCTUATION:
        return True
    words = _normalize_spoken_command(text).split()
    return (
        bool(words)
        and words[0] in _CONTINUATION_LEAD_WORDS
        and words[1:2] not in (["also"], ["then"])
    )


def _ends_with_words(text: str, tail: str) -> bool:
    words = _normalize_spoken_command(text or "").split()
    tail_words = _normalize_spoken_command(tail or "").split()
    return bool(tail_words) and words[-len(tail_words) :] == tail_words


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
class _CommentaryDelivery:
    """Commentary sent (or kept back) for one entry, until the model spoke it.

    ``held`` deliveries were never appended: the session was down. The rest
    went out bound to ``delegation_id`` and stay listed until the model
    starts speaking after them (``LiveDelegationBridge._confirm_deliveries``)
    or a reconnect re-appends them session-wide.
    """

    chunks: list[str]
    final: bool
    delegation_id: str | None
    at: float
    held: bool = False


@dataclass
class _HeldUtterance:
    """A closed caller utterance waiting briefly for its continuation."""

    text: str
    first_at: float
    item_ids: list[str] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None
    # A delegation the model emitted while the words were held; the turn
    # binds to it when the hold settles.
    delegation_id: str | None = None
    pending: str = ""
    # The words of a silent running turn these words continue.
    lead_in: str = ""


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
    """One caller utterance or an owned orphaned Super Agent result.

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
    # This prompt is the complete form of the request the client's running
    # turn started on (a split utterance, merged): ``run_turn`` replaces that
    # turn instead of queueing the request behind it.
    replaces_turn: bool = False
    record: Any = None
    turn_id: str | None = None
    superseded: bool = False
    completed: bool = False
    approval_notified: bool = False
    created_at: float = field(default_factory=time.monotonic)
    task: asyncio.Task[None] | None = None
    heartbeat: asyncio.Task[None] | None = None
    # Commentary the model has not been seen to speak yet (newest last).
    deliveries: list[_CommentaryDelivery] = field(default_factory=list)


class _CallLogAdapter(logging.LoggerAdapter):
    """Append ``call=<room>`` to every bridge log line so one call's lines can
    be pulled out of a service log that interleaves calls, and joined with the
    gateway's session lines and the Super Agents turn store."""

    def __init__(self, base: logging.Logger, call_id: str) -> None:
        super().__init__(base, {"call": call_id})
        self._suffix = f" call={call_id}" if call_id else ""

    def log(self, level, msg, *args, **kwargs):
        if self.isEnabledFor(level):
            msg, kwargs = self.process(msg, kwargs)
            suffix = self._suffix.replace("%", "%%") if args else self._suffix
            kwargs["stacklevel"] = kwargs.get("stacklevel", 1) + 1
            self.logger.log(level, f"{msg}{suffix}", *args, **kwargs)


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
        utterance_transcript_lag_seconds: float = UTTERANCE_TRANSCRIPT_LAG_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        call_id: str = "",
        initial_agent_label: str | None = None,
    ) -> None:
        self._voice_router = voice_router
        self._call_id = call_id
        self._log = _CallLogAdapter(logger, call_id)
        self._stats: Counter[str] = Counter(superseded=0)
        self._attached_at: float | None = None
        self._ledger = delivery_ledger
        self._developer_instructions = developer_instructions
        self._max_tokens = max_commentary_tokens
        self._heartbeat_interval = progress_thinking_interval
        self._settle_seconds = utterance_settle_seconds
        self._hold_max_seconds = utterance_hold_max_seconds
        self._transcript_lag_seconds = utterance_transcript_lag_seconds
        self._user_speaking = False
        self._last_speech_end: float | None = None
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
        # A call started from a project thread is already routed there when
        # the bridge is built; the dispatcher otherwise.
        self._active_agent_label = (
            initial_agent_label or ""
        ).strip() or DISPATCHER_AGENT_LABEL
        # When the plugin reported the gateway socket gone; None while it is up.
        self._session_down_at: float | None = None
        self._closed = False
        self.character_route_changed = None
        self._suspended_input_session = None
        self._suspended_input_handler = None
        self._input_route = None

    # wiring

    @property
    def active_agent_label(self) -> str:
        return self._active_agent_label

    def starting_agent_label(self) -> str | None:
        """The agent on the call from the start, None when it is the dispatcher."""
        if self._active_agent_label == DISPATCHER_AGENT_LABEL:
            return None
        return self._active_agent_label

    def _session_handlers(self) -> tuple[tuple[str, Callable[..., None]], ...]:
        return (
            ("input_audio_transcription_completed", self._on_input_transcription),
            ("delegation_created", self.on_delegation_created),
            ("session_reconnected", self.on_session_reconnected),
            ("error", self._on_session_error),
        )

    def attach(self, live_session) -> None:
        """Subscribe to the plugin session: closed caller utterances and delegations."""
        self.detach()
        self._live_session = live_session
        self._input_route = self._voice_router.route_snapshot()
        if self._attached_at is None:
            self._attached_at = self._clock()
        for event_name, handler in self._session_handlers():
            live_session.on(event_name, handler)

    def detach(self) -> None:
        if self._suspended_input_session is not None:
            self._suspended_input_session.off(
                "input_audio_transcription_completed", self._suspended_input_handler
            )
            self._suspended_input_session = None
            self._suspended_input_handler = None
        if self._live_session is not None:
            for event_name, handler in self._session_handlers():
                try:
                    self._live_session.off(event_name, handler)
                except Exception:
                    self._log.debug("live session off() failed", exc_info=True)
        self._live_session = None

    def suspend_session(self) -> None:
        """Hold backend deliveries while the immutable character is replaced."""
        if self._session_down_at is None:
            self._session_down_at = self._clock()
        old = self._live_session
        if self._speaking_record is not None:
            record, self._speaking_record = self._speaking_record, None
            if self._ledger is not None:
                self._ledger.mark_live_audio_finished(record, interrupted=True)
        if old is not None:
            route = self._input_route
            self.detach()

            def caller_input(event):
                if self._voice_router.can_deliver_for_snapshot(route):
                    self._on_input_transcription(event)

            old.on("input_audio_transcription_completed", caller_input)
            self._suspended_input_session = old
            self._suspended_input_handler = caller_input

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._log_call_summary()
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
        if self._session_down_at is not None:
            # The plugin queues this for the next session, whose delegations
            # are new: a bound append would only be rejected there.
            delegation_id = None
        chunks = chunk_commentary(
            text,
            max_tokens=self._max_tokens,
            speech_format=method == "append_commentary",
        )
        for chunk in chunks or [text]:
            try:
                getattr(self._live_session, method)(chunk, delegation_id=delegation_id)
            except Exception:
                self._log.warning(
                    "%s stage=live_append_failed method=%s delegation_id=%s",
                    DISPATCH_TIMING_LOG,
                    method,
                    delegation_id or "",
                    exc_info=True,
                )
                return
            self._stats[method] += 1
            self._log.info(
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
                text = join_fragments(open_entry.lead_in, text)
            self._dispatch_utterance(text, replaces_turn=True)
            return
        self._hold_utterance(text, item_id)

    def _dispatch_utterance(self, text: str, *, replaces_turn: bool = False) -> None:
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
        self._start_turn(text, source="transcript", replaces_turn=replaces_turn)

    # fragment settling

    def _hold_utterance(self, text: str, item_id: str | None) -> None:
        now = self._clock()
        held = self._held
        if held is None:
            held = self._held = _HeldUtterance(text=text, first_at=now)
            continued = self._continuable_entry(text)
            if continued is not None:
                held.lead_in = continued.prompt
            self._log_forced(
                text,
                decision="held_continuation" if held.lead_in else "held",
                key=continued.key if continued is not None else "",
            )
        else:
            held.text = join_fragments(held.text, text)
            held.pending = ""
            self._log_forced(held.text, decision="merged_fragment", key="")
        if item_id:
            held.item_ids.append(item_id)
        self._schedule_flush(self._settle_deadline(now))

    def _settle_deadline(self, now: float) -> float:
        held = self._held
        if held is not None and self._user_speaking:
            # The caller is still talking: wait for their words, bounded.
            return held.first_at + self._hold_max_seconds
        deadline = now + self._settle_seconds
        if self._last_speech_end is not None:
            deadline = max(
                deadline, self._last_speech_end + self._transcript_lag_seconds
            )
        return deadline

    def _continuable_entry(self, text: str) -> LiveDelegationEntry | None:
        """A silent turn, just started on the caller's words, that these continue."""
        if not looks_like_continuation(text) or is_trivial_utterance(text):
            return None
        client = self._voice_router.active_client
        entry = self._newest_entry_for(client)
        if entry is None or entry.completed or entry.open_utterance:
            return None
        if self._clock() - entry.created_at > CONTINUATION_SECONDS:
            return None
        cursor = self._cursors.get(entry.turn_id or entry.key)
        if cursor is not None and cursor.spoke_anything:
            return None
        return entry

    def on_user_state_changed(self, old_state: str, new_state: str) -> None:
        """The session VAD's view of the caller's voice (``user_state_changed``)."""
        if new_state == "speaking" and old_state != "speaking":
            self._user_speaking = True
            if self._held is not None:
                self._schedule_flush(self._held.first_at + self._hold_max_seconds)
        elif old_state == "speaking" and new_state != "speaking":
            self._user_speaking = False
            self._last_speech_end = self._clock()
            if self._held is not None:
                self._schedule_flush(
                    self._last_speech_end + self._transcript_lag_seconds
                )

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
        replaces_turn = bool(held.lead_in)
        if held.lead_in:
            held.text = join_fragments(held.lead_in, held.text)
        if held.delegation_id:
            self._start_turn_from_held(
                held, held.pending, held.delegation_id, replaces_turn=replaces_turn
            )
            return
        self._log_forced(held.text, decision="settled", key="")
        self._dispatch_utterance(held.text, replaces_turn=replaces_turn)

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
        self._stats["delegations_created"] += 1
        self._log.info(
            "%s stage=live_delegation_created delegation_id=%s pending_len=%d "
            "pending_hash=%s active_thread_id=%s",
            DISPATCH_TIMING_LOG,
            delegation_id,
            len(pending),
            _hash(pending) if pending else "",
            getattr(self._voice_router.active_client, "_thread_id", "") or "",
        )
        held = self._held
        if held is not None:
            if self._user_speaking:
                # The caller is still talking (session VAD): the model jumped
                # on half a request. Bind the delegation and let the hold
                # settle rather than start a turn on it (BUG 18).
                held.delegation_id = delegation_id
                if pending:
                    held.pending = pending
                self._log_forced(
                    held.text,
                    decision="delegation_held",
                    key="",
                    delegation_id=delegation_id,
                )
                return
            # The caller is silent and the model judged the request complete:
            # its delegation is a better end-of-request signal than our timer,
            # so the agent hears the request now (no added latency).
            held = self._take_held()
            replaces_turn = bool(held.lead_in)
            if held.lead_in:
                held.text = join_fragments(held.lead_in, held.text)
            self._start_turn_from_held(
                held, pending, delegation_id, replaces_turn=replaces_turn
            )
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
                text = (
                    join_fragments(open_entry.lead_in, pending)
                    if open_entry.lead_in
                    else pending
                )
                entry = self._start_turn(
                    text,
                    source="delegation",
                    delegation_id=delegation_id,
                    key=delegation_id,
                    open_utterance=pending,
                    replaces_turn=True,
                )
                entry.lead_in = open_entry.lead_in
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
        self,
        held: _HeldUtterance,
        pending: str,
        delegation_id: str,
        *,
        replaces_turn: bool = False,
    ) -> None:
        """The model delegated while fragments were held: one request, now."""
        if not pending or _ends_with_words(held.text, pending):
            # Nothing open, or its final already merged into the held words.
            text, open_utterance, lead_in = held.text, "", ""
        elif remainder_after(held.text, pending) is not None:
            # The model's open turn already contains the held words.
            text, open_utterance, lead_in = pending, pending, ""
        else:
            text = join_fragments(held.text, pending)
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
            replaces_turn=replaces_turn,
        )
        entry.lead_in = lead_in

    def on_session_reconnected(self, *_args) -> None:
        """The plugin opened a new GPT-Live session mid-call.

        Its delegations died with the old session: turns still running
        answer session-wide instead. A held utterance stays held (the plugin
        closes the caller's open speech before this, and that final arrives
        first). The model is briefed so it continues rather than greeting the
        caller again, and then hears what the caller missed: commentary kept
        back while the socket was down, and commentary bound to the dropped
        session that the model never started speaking after. Both go out
        session-wide, once, before the held utterance can start a new turn.
        """
        if self._closed:
            return
        drop_at, self._session_down_at = self._session_down_at, None
        unbound = 0
        running = 0
        if self._held is not None:
            self._held.delegation_id = None
            self._held.pending = ""
        for entry in self._entries.values():
            if entry.delegation_id is not None:
                entry.delegation_id = None
                unbound += 1
            entry.open_utterance = ""
            entry.lead_in = ""
            if not entry.completed and not entry.superseded:
                running += 1
        self._last_exit_command_at = None
        self._log.info(
            "%s stage=live_session_reconnected unbound=%d running=%d held=%d "
            "drop_seen=%s",
            DISPATCH_TIMING_LOG,
            unbound,
            running,
            1 if self._held is not None else 0,
            drop_at is not None,
        )
        pending = (
            LIVE_RECONNECTED_PENDING.format(label=self._active_agent_label)
            if running
            else ""
        )
        self._append_thinking(
            LIVE_RECONNECTED_THINKING.format(pending=pending).strip(), None
        )
        self._redeliver_after_reconnect(drop_at)

    def _on_session_error(self, event) -> None:
        """The plugin lost the gateway socket (a recoverable connection error).

        Every reconnect is preceded by one of these; a recoverable error
        that is not a connection error is the server rejecting one event
        (the socket is still up). Duck-typed on the exception class so the
        bridge needs no plugin import.
        """
        if self._closed or not getattr(event, "recoverable", False):
            return
        error = getattr(event, "error", None)
        if type(error).__name__ != "APIConnectionError":
            return
        if self._session_down_at is not None:
            return
        self._session_down_at = self._clock()
        self._log.info(
            "%s stage=live_session_dropped running=%d",
            DISPATCH_TIMING_LOG,
            sum(
                1
                for e in self._entries.values()
                if not e.completed and not e.superseded
            ),
        )

    def _deliver(
        self, entry: LiveDelegationEntry, chunks: list[str], *, final: bool
    ) -> None:
        """Commentary for one entry: spoken now, or kept for the next session."""
        if not chunks:
            return
        delivery = _CommentaryDelivery(
            chunks=list(chunks),
            final=final,
            delegation_id=entry.delegation_id,
            at=self._clock(),
            held=self._session_down_at is not None,
        )
        cutoff = (
            self._session_down_at
            if self._session_down_at is not None
            else delivery.at
        ) - REDELIVERY_MAX_AGE_SECONDS
        entry.deliveries = [
            pending
            for pending in entry.deliveries
            if pending.held or pending.at >= cutoff
        ]
        entry.deliveries.append(delivery)
        if delivery.held:
            self._log.info(
                "%s stage=live_commentary_held key=%s delegation_id=%s final=%s "
                "chunks=%d",
                DISPATCH_TIMING_LOG,
                entry.key,
                entry.delegation_id or "",
                final,
                len(chunks),
            )
            return
        for chunk in chunks:
            self._append_commentary(chunk, entry.delegation_id)

    def _confirm_deliveries(self) -> None:
        """The model started speaking: bound commentary before that was spoken."""
        if self._session_down_at is not None:
            return
        cutoff = self._clock() - DELIVERY_CONFIRM_MIN_SECONDS
        for entry in self._entries.values():
            if entry.deliveries:
                entry.deliveries = [
                    d for d in entry.deliveries if d.held or d.at > cutoff
                ]

    def _redeliver_after_reconnect(self, drop_at: float | None) -> None:
        """Re-append, session-wide, what the caller did not hear before the drop.

        Held deliveries always qualify. A delivery that went out does when
        the model never started speaking after it and it is recent enough:
        bound ones died with their delegation; unbound ones only if they were
        sent before the drop (sent during it, the plugin itself replays them
        into the new session). Without a drop seen, only the bound ones.
        """
        now = self._clock()
        oldest = (drop_at if drop_at is not None else now) - REDELIVERY_MAX_AGE_SECONDS
        for entry in self._entries.values():
            due = [
                d
                for d in entry.deliveries
                if d.held
                or (
                    d.at >= oldest
                    and (
                        d.delegation_id is not None
                        or (drop_at is not None and d.at < drop_at)
                    )
                )
            ]
            entry.deliveries = []
            if not due or entry.superseded:
                continue
            if not self._voice_router.can_deliver_for_snapshot(entry.route):
                continue
            chunks = [chunk for d in due for chunk in d.chunks]
            label = self._active_agent_label
            disposition = (
                LIVE_REDELIVERY_ANSWER
                if entry.completed
                else LIVE_REDELIVERY_PROGRESS.format(label=label)
            )
            self._log.info(
                "%s stage=live_commentary_redelivered key=%s chunks=%d held=%d "
                "bound=%d answer=%s",
                DISPATCH_TIMING_LOG,
                entry.key,
                len(chunks),
                sum(1 for d in due if d.held),
                sum(1 for d in due if not d.held and d.delegation_id is not None),
                entry.completed,
            )
            self._append_thinking(
                LIVE_RECONNECTED_REDELIVERY.format(
                    label=label, disposition=disposition
                ),
                None,
            )
            # Tracked again: a second drop before the model speaks it loses it
            # otherwise.
            entry.deliveries.append(
                _CommentaryDelivery(
                    chunks=chunks,
                    final=entry.completed,
                    delegation_id=None,
                    at=now,
                )
            )
            for chunk in chunks:
                self._append_commentary(chunk, None)

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
        self._log.info(
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
        replaces_turn: bool = False,
    ) -> LiveDelegationEntry:
        """Run (or steer) the active agent's turn on one caller utterance.

        With ``replaces_turn`` the text is the complete request the client's
        running turn began on; ``run_turn`` merges into or replaces that turn
        rather than queueing a second execution behind it.
        """
        client = self._voice_router.active_client
        route = self._voice_router.route_snapshot()
        inherited: LiveDelegationEntry | None = None
        pending_deliveries: list[_CommentaryDelivery] = []
        for other in self._entries.values():
            if other.client is not client or other.superseded:
                continue
            other.superseded = True
            self._stats["superseded"] += 1
            if not other.completed and other.route.same_route(route):
                pending_deliveries.extend(other.deliveries)
                other.deliveries = []
            # run_turn steers the running turn, so its merged answer also
            # answers the delegation the superseded utterance was bound to.
            if (
                not other.completed
                and other.delegation_id
                and (inherited is None or other.created_at >= inherited.created_at)
            ):
                inherited = other
            self._log.info(
                "%s stage=live_delegation_superseded key=%s delegation_id=%s "
                "replaced=%s",
                DISPATCH_TIMING_LOG,
                other.key,
                other.delegation_id or "",
                replaces_turn and not other.completed,
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
            replaces_turn=replaces_turn,
            created_at=self._clock(),
            deliveries=pending_deliveries,
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
        self._log.info(
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
        self._stats[f"decision_{decision}"] += 1
        self._log.info(
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
        if new_state == "speaking" and old_state != "speaking":
            self._confirm_deliveries()
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
        """Invalidate old speech and hand the new route to the character owner."""
        label = (agent_label or "").strip() or DISPATCHER_AGENT_LABEL
        if action == "exit_to_dispatch":
            label = DISPATCHER_AGENT_LABEL
        self._active_agent_label = label
        self._reset_utterance_state()
        if self.character_route_changed is not None:
            for entry in self._entries.values():
                if not self._voice_router.can_deliver_for_snapshot(entry.route):
                    entry.superseded = True
            self.character_route_changed()
            return
        instructions = ""
        try:
            instructions = self._developer_instructions() or ""
        except Exception:
            self._log.debug("live route instructions unavailable", exc_info=True)
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
        if self._closed or not speech_text or not turn_id:
            return
        matching = [
            entry
            for entry in self._entries.values()
            if entry.client is client and entry.turn_id == turn_id
        ]
        current = [
            entry
            for entry in matching
            if not entry.superseded
            and self._voice_router.can_deliver_for_snapshot(entry.route)
        ]
        if matching and not current:
            return
        if not self._voice_router.claim_speech(client, turn_id):
            return
        cursor = self._cursor(turn_id)
        chunks = cursor.advance(speech_text, final=True)
        if not chunks:
            return
        if current:
            entry = max(current, key=lambda item: item.created_at)
        else:
            self._utterance_seq += 1
            entry = LiveDelegationEntry(
                key=f"orphan-{self._utterance_seq}",
                prompt="",
                route=self._voice_router.route_snapshot(),
                client=client,
                source="orphaned_result",
                turn_id=turn_id,
                completed=True,
                created_at=self._clock(),
            )
            self._entries[entry.key] = entry
            self._prune_entries()
        self._deliver(entry, chunks, final=True)

    # delegation execution

    async def _run_delegation(self, entry: LiveDelegationEntry) -> None:
        prompt = wrap_voice_prompt(entry.prompt)
        if self._voice_router.is_dispatcher_active:
            prompt = append_onboarding_reminder(prompt)
        prompt = apply_screen_context(self._voice_router, prompt)
        entry.heartbeat = asyncio.create_task(
            self._progress_heartbeat(entry),
            name=f"openbase-live-heartbeat-{entry.key}",
        )
        try:
            result = await entry.client.run_turn(
                prompt,
                developer_instructions=self._developer_instructions(),
                replaces_active_turn=entry.replaces_turn,
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
            if self._session_down_at is not None:
                continue
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
        self._stats["turns_failed"] += 1
        self._log.exception(
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
        self._deliver(
            entry,
            [
                LIVE_BACKEND_BUSY_COMMENTARY
                if busy
                else LIVE_BACKEND_UNRESPONSIVE_COMMENTARY
            ],
            final=True,
        )

    def _on_turn_result(self, entry: LiveDelegationEntry, result: dict) -> None:
        entry.completed = True
        turn_id = str(result.get("_livekit_turn_id") or "")
        speech_text = str(result.get("_livekit_speech_text") or "")
        if turn_id and entry.turn_id != turn_id:
            self._log_turn_bound(entry, turn_id, source="result")
        entry.turn_id = turn_id or None
        ledger = self._ledger
        if ledger is not None and entry.record is not None and turn_id:
            ledger.mark_answer_owed(entry.record, turn_id=turn_id, client=entry.client)
        if entry.superseded:
            self._log.info(
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
            self._log.info(
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
            self._deliver(entry, chunks, final=True)
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
        self._deliver(entry, [LIVE_EMPTY_ANSWER_COMMENTARY], final=True)
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
            self._log_turn_bound(entry, turn_id, source="progress")
        if not self._voice_router.can_deliver_for_snapshot(entry.route):
            return
        if _progress_has_pending_requests(progress) and not entry.approval_notified:
            entry.approval_notified = True
            self._deliver(entry, [LIVE_APPROVAL_PENDING_COMMENTARY], final=False)
            self._append_instructions(
                LIVE_APPROVAL_PENDING_INSTRUCTIONS, entry.delegation_id
            )
        # Turn-scoped: a running turn's snapshot still carries the PREVIOUS
        # answer at session level, and relaying it as commentary made GPT-Live
        # speak the last question's answer before the new one.
        text = _speech_text_from_progress(progress, turn_scoped=True, turn_id=turn_id)
        if not text or _looks_like_raw_backend_error(text):
            return
        self._deliver(
            entry, self._cursor(turn_id).advance(text, final=False), final=False
        )

    # correlation

    def _log_turn_bound(
        self, entry: LiveDelegationEntry, turn_id: str, *, source: str
    ) -> None:
        """One line per utterance → Super Agents turn, so a turn row in the
        store can be traced back to the spoken request that started it."""
        self._stats["turns_bound"] += 1
        self._log.info(
            "%s stage=live_delegation_turn_bound key=%s turn_id=%s delegation_id=%s "
            "source=%s active_thread_id=%s",
            DISPATCH_TIMING_LOG,
            entry.key,
            turn_id,
            entry.delegation_id or "",
            source,
            getattr(entry.client, "_thread_id", "") or "",
        )

    def _log_call_summary(self) -> None:
        """Counts for the whole call, logged once when the bridge closes."""
        duration = (
            self._clock() - self._attached_at if self._attached_at is not None else 0.0
        )
        self._stats["utterances"] = self._stats["decision_started"]
        counts = " ".join(
            f"{name}={count}" for name, count in sorted(self._stats.items())
        )
        self._log.info(
            "%s stage=live_call_summary duration_s=%.1f %s",
            DISPATCH_TIMING_LOG,
            max(duration, 0.0),
            counts,
        )

    # helpers

    def _exit_to_dispatch(self, *, delegation_id: str | None) -> None:
        changed = self._voice_router.exit_to_dispatch()
        for entry in self._entries.values():
            if not entry.superseded:
                entry.superseded = True
                self._stats["superseded"] += 1
        self._active_agent_label = DISPATCHER_AGENT_LABEL
        self._reset_utterance_state()
        if changed and self.character_route_changed is not None:
            self.character_route_changed()
        elif changed or delegation_id is not None:
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
            if (
                entry.client is client
                and not entry.superseded
                and entry.source != "orphaned_result"
            ):
                if newest is None or entry.created_at >= newest.created_at:
                    newest = entry
        return newest

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
            if (
                entry.superseded
                or entry.delegation_id
                or entry.created_at < cutoff
                or entry.source == "orphaned_result"
            ):
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
