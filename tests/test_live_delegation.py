"""Tier-1 tests for the GPT-Live client delegation bridge (no network)."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from types import SimpleNamespace

import pytest
from livekit.agents import APIConnectionError, APIError

from openbase_coder_cli.livekit_agent import live_delegation
from openbase_coder_cli.livekit_agent.live_delegation import (
    BACK_TO_DISPATCH_COMMENTARY,
    COMMENTARY_MAX_TOKENS,
    LIVE_APPROVAL_PENDING_COMMENTARY,
    LIVE_BACKEND_BUSY_COMMENTARY,
    LIVE_BACKEND_UNRESPONSIVE_COMMENTARY,
    LIVE_EMPTY_ANSWER_COMMENTARY,
    LiveDelegationBridge,
    LiveSpeechCursor,
    chunk_commentary,
    estimate_tokens,
    is_trivial_utterance,
    remainder_after,
)
from openbase_coder_cli.livekit_agent.screen_context import FocusedThread
from openbase_coder_cli.livekit_agent.voice_delivery import (
    VoiceDeliveryLedger,
    VoiceRouteSnapshot,
)
from openbase_coder_cli.voice_tags import (
    VOICE_TAG_CLOSE,
    VOICE_TAG_OPEN,
    wrap_voice_prompt,
)


class FakeGPTLiveSession:
    """A ``GPTLiveSession``-like emitter: the three appends plus events."""

    def __init__(self) -> None:
        self.appends: list[tuple[str, str, str | None]] = []
        self._handlers: dict[str, list] = {}
        self._speech_ids = itertools.count(1)

    def on(self, event, callback):
        self._handlers.setdefault(event, []).append(callback)
        return callback

    def off(self, event, callback):
        self._handlers.get(event, []).remove(callback)

    def emit(self, event, *args):
        for callback in list(self._handlers.get(event, [])):
            callback(*args)

    def append_thinking(self, text, *, delegation_id=None):
        self.appends.append(("thinking", text, delegation_id))

    def append_commentary(self, text, *, delegation_id=None):
        self.appends.append(("commentary", text, delegation_id))

    def append_instructions(self, text, *, delegation_id=None):
        self.appends.append(("instructions", text, delegation_id))

    def delegate(self, delegation_id: str, pending_transcript: str = "") -> None:
        self.emit(
            "delegation_created",
            SimpleNamespace(id=delegation_id, pending_transcript=pending_transcript),
        )

    def final(self, transcript: str, item_id: str | None = None) -> None:
        """The plugin closed a caller utterance (``_end_speech``)."""
        self.emit(
            "input_audio_transcription_completed",
            SimpleNamespace(
                item_id=item_id or f"speech_{next(self._speech_ids)}",
                transcript=transcript,
                is_final=True,
            ),
        )

    def partial(self, transcript: str, item_id: str = "speech_open") -> None:
        self.emit(
            "input_audio_transcription_completed",
            SimpleNamespace(item_id=item_id, transcript=transcript, is_final=False),
        )

    def drop(self) -> None:
        """The gateway socket closed: the plugin's recoverable error before it retries."""
        self.emit(
            "error",
            SimpleNamespace(
                error=APIConnectionError("GPT-Live connection closed unexpectedly"),
                recoverable=True,
            ),
        )

    def server_error(self) -> None:
        """The server rejected one event; the socket is still up."""
        self.emit(
            "error",
            SimpleNamespace(
                error=APIError("GPT-Live returned an error", retryable=True),
                recoverable=True,
            ),
        )

    def speech(self, delegation_id=...) -> list[str]:
        """Requested spoken text, distinct from model audio/playback evidence."""
        prefix = (
            "Read this next backend answer segment aloud exactly once and in full. "
        )
        result = []
        for method, text, d_id in self.appends:
            if delegation_id is not ... and d_id != delegation_id:
                continue
            if method == "commentary":
                result.append(text)
            elif method == "instructions" and text.startswith(prefix):
                result.append(json.loads(text.split("Text to read: ", 1)[1]))
        return result

    def of(self, kind: str, delegation_id=...) -> list[str]:
        return [
            text
            for method, text, d_id in self.appends
            if method == kind and (delegation_id is ... or d_id == delegation_id)
        ]


class FakeVoiceClient:
    """Stands in for ``SuperAgentsLiveKitClient.run_turn``."""

    def __init__(self, *, thread_id="thread-1") -> None:
        self._thread_id = thread_id
        self.prompts: list[tuple[str, str | None]] = []
        # Parallel to ``prompts``: whether each run replaced the running turn.
        self.replaces: list[bool] = []
        self.listeners: list = []
        self.claimed: list[str] = []
        self.result_gate = asyncio.Event()
        self.result: dict | Exception = {
            "_livekit_speech_text": "All tests pass. The build is green.",
            "_livekit_turn_id": "turn-1",
            "status": "completed",
            "progress": {},
        }
        self.busy = False

    async def run_turn(
        self, prompt, *, developer_instructions=None, replaces_active_turn=False
    ):
        self.prompts.append((prompt, developer_instructions))
        self.replaces.append(replaces_active_turn)
        await self.result_gate.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return dict(self.result)

    def add_turn_progress_listener(self, listener):
        self.listeners.append(listener)

    def remove_turn_progress_listener(self, listener):
        self.listeners.remove(listener)

    def claim_speech(self, turn_id):
        if turn_id in self.claimed:
            return False
        self.claimed.append(turn_id)
        return True

    def backend_appears_busy(self):
        return self.busy

    def progress(self, turn_id, progress):
        for listener in list(self.listeners):
            listener(self, turn_id, progress)


class FakeVoiceRouter:
    def __init__(self, dispatcher: FakeVoiceClient) -> None:
        self._dispatcher = dispatcher
        self.active_client = dispatcher
        self.route_version = 0
        self.exits = 0

    @property
    def is_dispatcher_active(self):
        return self.active_client is self._dispatcher

    def route_snapshot(self):
        return VoiceRouteSnapshot(
            route_version=self.route_version,
            active_thread_id=self.active_client._thread_id,
            active_voice_id=None,
            active_voice_name=None,
            active_route="dispatcher" if self.is_dispatcher_active else "codex_thread",
        )

    def can_deliver_for_snapshot(self, snapshot):
        return snapshot.same_route(self.route_snapshot())

    def exit_to_dispatch(self):
        self.exits += 1
        if self.is_dispatcher_active:
            return False
        self.active_client = self._dispatcher
        self.route_version += 1
        return True

    def transfer(self, client):
        self.active_client = client
        self.route_version += 1

    def claim_speech(self, client, turn_id):
        return self.active_client is client and client.claim_speech(turn_id)


def _make_bridge(
    *,
    ledger=True,
    developer_instructions="Direct route guidance.",
    clock=None,
    settle=0.0,
    hold_max=3.0,
    lag=0.0,
    call_id="",
    barge_in_min=0.0,
    thread_exchange_fetcher=None,
):
    dispatcher = FakeVoiceClient(thread_id="dispatcher-thread")
    router = FakeVoiceRouter(dispatcher)
    lifecycle: list[tuple[str, str]] = []
    delivery_ledger = None
    if ledger:
        delivery_ledger = VoiceDeliveryLedger(
            route_snapshot=router.route_snapshot, room_name="room", live_mode=True
        )
        delivery_ledger.set_lifecycle_sink(
            lambda event, record, reason: lifecycle.append((event, record.message_id))
        )
    live = FakeGPTLiveSession()
    bridge = LiveDelegationBridge(
        voice_router=router,
        delivery_ledger=delivery_ledger,
        developer_instructions=lambda: developer_instructions,
        progress_thinking_interval=3600,
        utterance_settle_seconds=settle,
        utterance_hold_max_seconds=hold_max,
        utterance_transcript_lag_seconds=lag,
        call_id=call_id,
        **({"clock": clock} if clock is not None else {}),
        **(
            {"thread_exchange_fetcher": thread_exchange_fetcher}
            if thread_exchange_fetcher is not None
            else {"thread_exchange_fetcher": _no_exchanges}
        ),
    )
    bridge.attach(live)
    # Caller speech interrupts at once unless a test exercises the echo debounce.
    bridge.speech_gate.barge_in_min_seconds = barge_in_min
    return bridge, live, router, dispatcher, delivery_ledger, lifecycle


async def _no_exchanges(thread_id: str):
    # Tests that are not about the thread brief get a quiet voice session.
    return []


async def _settle():
    # A zero-delay utterance hold flushes on a loop timer, one iteration later.
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.sleep(0.001)
    for _ in range(5):
        await asyncio.sleep(0)


# --- chunking ---------------------------------------------------------------


def test_chunk_commentary_respects_the_500_token_cap_and_sentence_bounds():
    text = " ".join(f"Sentence number {i} ends here." for i in range(300))
    chunks = chunk_commentary(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert estimate_tokens(chunk) <= COMMENTARY_MAX_TOKENS
        assert chunk.endswith(".")
    assert " ".join(chunks).count("ends here.") == 300


def test_chunk_commentary_splits_an_oversized_sentence_at_word_boundaries():
    text = "word " * 2000
    chunks = chunk_commentary(text.strip(), max_tokens=100)
    assert len(chunks) > 1
    assert all(estimate_tokens(chunk) <= 100 for chunk in chunks)
    assert all(not chunk.startswith(" ") and "  " not in chunk for chunk in chunks)


def test_chunk_commentary_applies_speech_formatting_but_thinking_stays_verbatim():
    spoken = chunk_commentary("Edited `config.py` in **bold**.")
    assert spoken and "`" not in spoken[0] and "**" not in spoken[0]
    plain = chunk_commentary('Request taken: "edit `config.py`".', speech_format=False)
    assert plain == ['Request taken: "edit `config.py`".']


def test_speech_cursor_streams_only_new_complete_sentences_then_the_rest():
    cursor = LiveSpeechCursor()
    assert cursor.advance("Reading the file. Found two", final=False) == [
        "Reading the file."
    ]
    assert cursor.advance("Reading the file. Found two", final=False) == []
    assert cursor.advance(
        "Reading the file. Found two bugs. Fixing them now.", final=True
    ) == ["Found two bugs. Fixing them now."]
    assert (
        cursor.advance("Reading the file. Found two bugs. Fixing them now.", final=True)
        == []
    )


# --- utterance matching and the trivial-utterance filter ----------------------


def test_remainder_after_compares_normalized_words_and_keeps_original_wording():
    assert remainder_after("Check the build", "check the build.") == ""
    assert (
        remainder_after("and run the tests", "And run the tests, please, twice.")
        == "please, twice"
    )
    assert remainder_after("What's on my", "What's on my desktop?") == "desktop?"
    assert remainder_after("Check the bill", "Check the build") is None
    assert remainder_after("Check the build and deploy", "Check the build") is None
    assert remainder_after("", "anything") == "anything"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "...",
        "Hello.",
        "hello?",
        "Hey there!",
        "Thanks.",
        "Thank you so much.",
        "Okay.",
        "OK, thanks!",
        "Mm-hmm.",
        "Uh huh",
        "Um...",
        "Got it, cool.",
        "Alright, sounds good.",
        "Bye bye.",
        "Hi, good morning.",
    ],
)
def test_trivial_utterances_skip_the_agent(text):
    assert is_trivial_utterance(text)


@pytest.mark.parametrize(
    "text",
    [
        "What's on my desktop?",
        "what is on my desktop",
        "Yes.",
        "Yeah.",
        "No.",
        "Sure.",
        "Go ahead.",
        "Do it.",
        "Stop.",
        "Wait.",
        "Cancel that.",
        "Hello, what's on my calendar?",
        "Thanks, now run the tests.",
        "Okay so check the build",
        "Hey, are you there?",
        "Can you hear me?",
        "What time is it?",
        "okay okay okay okay okay okay okay",
    ],
)
def test_substantive_utterances_go_to_the_agent(text):
    assert not is_trivial_utterance(text)


# --- delegation <-> turn binding ---------------------------------------------


async def test_delegation_runs_a_voice_tagged_turn_and_streams_the_answer():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()

    live.delegate("d1", "Check whether the build passes")
    await _settle()

    assert dispatcher.prompts, "the thread turn did not start"
    prompt, instructions = dispatcher.prompts[0]
    assert prompt.endswith(f"Check whether the build passes{VOICE_TAG_CLOSE}")
    assert VOICE_TAG_OPEN in prompt
    assert instructions == "Direct route guidance."
    # Acceptance goes to thinking immediately, bound to the delegation.
    thinking = live.of("thinking", "d1")
    assert thinking and "Check whether the build passes" in thinking[0]
    assert live.speech() == []
    assert ("utterance_accepted", "live-d1") in lifecycle

    dispatcher.result_gate.set()
    await _settle()

    commentary = live.speech("d1")
    assert commentary == ["All tests pass. The build is green."]
    assert dispatcher.claimed == ["turn-1"]
    record = ledger.record_for_turn("turn-1")
    assert record is not None and record.message_id == "live-d1"
    assert record.speech_len == len("All tests pass. The build is green.")
    await bridge.aclose()


async def test_progress_snapshots_stream_commentary_before_the_turn_finishes():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Run the tests")
    await _settle()

    dispatcher.progress(
        "turn-1",
        {
            "status": "running",
            "summary": {
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "finalAnswer",
                        "text": "Tests are running. Twelve passed so far",
                    }
                ]
            },
        },
    )
    # The snapshot text is speech-formatted upstream (terminal period added),
    # so both sentences count as complete and stream right away.
    assert live.speech("d1") == ["Tests are running. Twelve passed so far."]

    dispatcher.result = {
        "_livekit_speech_text": "Tests are running. Twelve passed so far. All done.",
        "_livekit_turn_id": "turn-1",
        "status": "completed",
        "progress": {},
    }
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == [
        "Tests are running. Twelve passed so far.",
        "All done.",
    ]
    await bridge.aclose()


PREVIOUS_ANSWER = "Victoria already got back to you. She said two plus two is four."


def _running_snapshot(turn_id="turn-1", **turn_fields):
    """A Claude Code backend snapshot of a turn still in progress.

    The session-level ``lastUsefulMessage`` (and the session's turn history)
    still hold the PREVIOUS turn's answer while the new turn runs.
    """
    running = {"turnId": turn_id, "status": "running", **turn_fields}
    return {
        "status": "running",
        "turnId": turn_id,
        "lastUsefulMessage": PREVIOUS_ANSWER,
        "summary": {"lastUsefulMessage": PREVIOUS_ANSWER},
        "turn": running,
        "turns": [
            {
                "turnId": "turn-0",
                "status": "completed",
                "lastUsefulMessage": PREVIOUS_ANSWER,
            },
            running,
        ],
        "recentTurns": [
            {
                "turnId": "turn-0",
                "status": "completed",
                "lastUsefulMessage": PREVIOUS_ANSWER,
            }
        ],
    }


async def test_progress_never_relays_the_previous_turns_answer_as_commentary():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "What files are on my desktop")
    await _settle()

    dispatcher.progress("turn-1", _running_snapshot())

    assert live.speech() == []
    dispatcher.result_gate.set()
    await _settle()
    await bridge.aclose()


async def test_progress_streams_the_current_turns_own_text():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "What files are on my desktop")
    await _settle()

    dispatcher.progress(
        "turn-1", _running_snapshot(lastUsefulMessage="Checking your desktop now.")
    )

    assert live.speech("d1") == ["Checking your desktop now."]
    dispatcher.result_gate.set()
    await _settle()
    await bridge.aclose()


async def test_desktop_question_after_an_answered_question_speaks_only_the_new_answer():
    """Regression for the 2026-10-08 05:27Z staging call.

    The previous turn's answer was still cached at session level when the
    caller asked about the desktop; the first progress snapshot's stale text
    went out as commentary and GPT-Live said "Victoria already got back to
    you" before the desktop answer.
    """
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    _answer(dispatcher, "Your desktop is empty.")
    live.delegate("d1", "What files are on my desktop")
    await _settle()

    dispatcher.progress("turn-1", _running_snapshot())
    dispatcher.progress("turn-1", _running_snapshot())
    completed = _running_snapshot(lastUsefulMessage="Your desktop is empty.")
    completed["status"] = completed["turn"]["status"] = "completed"
    completed["lastUsefulMessage"] = "Your desktop is empty."
    dispatcher.progress("turn-1", completed)
    dispatcher.result_gate.set()
    await _settle()

    assert live.speech() == ["Your desktop is empty."]
    await bridge.aclose()


async def test_answer_streamed_by_progress_is_not_followed_by_an_already_answered_note():
    """Regression for the 2026-10-08 06:06Z staging call.

    Progress relayed the turn's own answer as commentary; the final result
    repeated it, and the bridge then appended the thinking note "That request
    was already answered; nothing new to add." GPT-Live spoke that note
    instead of the answer, three times in one call.
    """
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    answer = "The grocery list has three items: milk, eggs, and bread."
    _answer(dispatcher, answer)
    live.delegate("d1", "What's in the grocery list")
    await _settle()

    dispatcher.progress("turn-1", _running_snapshot(lastUsefulMessage=answer))
    dispatcher.result_gate.set()
    await _settle()

    assert live.speech("d1") == [answer]
    assert not any("already answered" in note for note in live.of("thinking", "d1"))
    record = ledger.record_for_turn("turn-1")
    assert record is not None
    assert record.status == "text_generated"
    assert record.terminal_reason is None
    assert not record.delivered
    assert ledger.has_pending_delivery_for_current_route()
    bridge.on_agent_state_changed("thinking", "speaking")
    assert bridge._speaking_record is record
    bridge.on_agent_state_changed("speaking", "listening")
    assert record.delivered
    await bridge.aclose()


# --- fragment settling ------------------------------------------------------


def _voice(prompt: str) -> str:
    return wrap_voice_prompt(prompt)


async def test_closed_utterance_waits_the_settle_window_before_the_agent_hears_it():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(settle=0.08)
    live.final("What files are on my desktop")
    await _settle()
    assert dispatcher.prompts == []
    await asyncio.sleep(0.15)
    assert len(dispatcher.prompts) == 1
    assert dispatcher.prompts[0][0].endswith(_voice("What files are on my desktop"))
    await bridge.aclose()


async def test_fragmented_question_reaches_the_agent_as_one_utterance():
    """Regression for the 2026-10-08 05:59Z staging call.

    The plugin closed "What files are on my" (0.8 s of audio with no new
    transcript fragment) and "desktop" arrived as a second utterance about a
    second later; each started its own agent turn and the dispatcher answered
    "I didn't catch the end of that".
    """
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(settle=0.08)
    live.final("What files are on my")
    await asyncio.sleep(0.03)
    live.final("desktop")
    await asyncio.sleep(0.15)
    assert len(dispatcher.prompts) == 1
    assert dispatcher.prompts[0][0].endswith(_voice("What files are on my desktop"))
    await bridge.aclose()


async def test_a_fragment_opening_during_the_hold_extends_it_until_it_closes():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        settle=0.04, hold_max=0.6
    )
    live.final("What files are on my", item_id="speech_1")
    live.partial("desk", item_id="speech_2")
    await asyncio.sleep(0.1)
    assert dispatcher.prompts == []
    live.final("desktop", item_id="speech_2")
    await asyncio.sleep(0.1)
    assert len(dispatcher.prompts) == 1
    assert dispatcher.prompts[0][0].endswith(_voice("What files are on my desktop"))
    await bridge.aclose()


async def test_the_hold_is_bounded_when_an_open_fragment_never_closes():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        settle=0.04, hold_max=0.12
    )
    live.final("What files are on my", item_id="speech_1")
    live.partial("desk", item_id="speech_2")
    await asyncio.sleep(0.25)
    assert len(dispatcher.prompts) == 1
    assert dispatcher.prompts[0][0].endswith(_voice("What files are on my"))
    await bridge.aclose()


async def test_a_delegation_during_the_hold_sends_the_held_words_with_its_pending_text():
    """Regression for the 2026-10-08 06:36Z staging call.

    "What's in the grocery list file on my" was closed, then the model
    delegated with the open fragment "desktop" pending; the held words and
    the pending fragment are one request, bound to that delegation, and the
    fragment's own final transcript is then covered.
    """
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(settle=0.05)
    live.final("What's in the grocery list file on my", item_id="speech_1")
    live.delegate("d1", "desktop")
    await _settle()
    await asyncio.sleep(0.12)
    assert len(dispatcher.prompts) == 1
    assert dispatcher.prompts[0][0].endswith(
        _voice("What's in the grocery list file on my desktop")
    )
    live.final("desktop", item_id="speech_2")
    await _settle()
    assert len(dispatcher.prompts) == 1
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == ["All tests pass. The build is green."]
    await bridge.aclose()


async def test_a_delegation_with_nothing_pending_during_the_hold_takes_the_held_words():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(settle=0.05)
    live.final("Run the tests")
    live.delegate("d1", "")
    await _settle()
    await asyncio.sleep(0.12)
    assert len(dispatcher.prompts) == 1
    assert dispatcher.prompts[0][0].endswith(_voice("Run the tests"))
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == ["All tests pass. The build is green."]
    await bridge.aclose()


async def test_words_after_a_delegated_fragment_steer_with_the_whole_request():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(settle=0.05)
    live.final("What's in the grocery list file on my", item_id="speech_1")
    live.delegate("d1", "desktop")
    await _settle()
    await asyncio.sleep(0.12)
    live.final("desktop, the one from yesterday", item_id="speech_2")
    await _settle()
    assert len(dispatcher.prompts) == 2
    assert dispatcher.prompts[1][0].endswith(
        _voice("What's in the grocery list file on my desktop, the one from yesterday")
    )
    await bridge.aclose()


# --- reconnects ---------------------------------------------------------------


async def test_a_reconnect_unbinds_dead_delegations_and_answers_session_wide():
    """Regression for the 2026-10-08 04:58Z staging call.

    A Cloud deploy replaced the relay task; the plugin reconnected and opened
    a new GPT-Live session, whose history is reseeded but whose delegations
    are new. The running turn's answer must not be bound to the dead id.
    """
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Run the tests")
    await _settle()
    assert len(dispatcher.prompts) == 1

    live.emit("session_reconnected")
    dispatcher.result_gate.set()
    await _settle()

    assert live.speech("d1") == []
    assert live.speech(None) == ["All tests pass. The build is green."]
    (briefing,) = [t for t in live.of("thinking", None) if "re-established" in t]
    assert "Do not greet the caller again" in briefing
    assert "the dispatcher is already answering" in briefing
    assert "do not ask the caller to repeat" in briefing
    await bridge.aclose()


async def test_a_reconnect_with_nothing_running_briefs_without_a_pending_note():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.emit("session_reconnected")
    await _settle()
    (briefing,) = live.of("thinking", None)
    assert "re-established" in briefing
    assert "already answering" not in briefing
    assert "continue from where they were" in briefing
    assert dispatcher.prompts == []
    await bridge.aclose()


async def test_an_utterance_closed_by_the_reconnect_still_reaches_the_agent():
    # The plugin closes the caller's open speech (a final transcript) right
    # before it emits session_reconnected.
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(settle=0.05)
    live.final("What files are on my desktop")
    live.emit("session_reconnected")
    await asyncio.sleep(0.12)
    assert len(dispatcher.prompts) == 1
    assert dispatcher.prompts[0][0].endswith(_voice("What files are on my desktop"))
    await bridge.aclose()


def _progress_snapshot(text: str, turn_id: str = "turn-1") -> dict:
    return {
        "status": "running",
        "turnId": turn_id,
        "summary": {
            "items": [{"type": "agentMessage", "phase": "finalAnswer", "text": text}]
        },
    }


def _redelivery_notes(live: FakeGPTLiveSession) -> list[str]:
    return [t for t in live.of("thinking", None) if "before the caller heard" in t]


async def test_commentary_produced_during_the_gap_is_spoken_once_after_reconnect():
    """Progress the turn streams while the socket is down reaches the new session.

    Appended during the gap it would be queued bound to the dead delegation
    and rejected by the new session ("Unknown client delegation").
    """
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Run the tests")
    await _settle()
    live.drop()
    dispatcher.progress("turn-1", _progress_snapshot("Tests are running. Six passed."))
    assert live.speech() == []

    live.emit("session_reconnected")
    assert live.speech("d1") == []
    assert live.speech(None) == ["Tests are running. Six passed."]
    (note,) = _redelivery_notes(live)
    assert "progress on the caller's last request" in note
    assert "the dispatcher is still working" in note
    thinking = live.of("thinking", None)
    assert thinking.index(next(t for t in thinking if "re-established" in t)) < (
        thinking.index(note)
    )

    # The rest of the turn is spoken once, session-wide, as before.
    dispatcher.result = {
        "_livekit_speech_text": "Tests are running. Six passed. All green.",
        "_livekit_turn_id": "turn-1",
        "status": "completed",
        "progress": {},
    }
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech(None) == [
        "Tests are running. Six passed.",
        "All green.",
    ]
    await bridge.aclose()


async def test_final_produced_during_the_gap_is_spoken_once_as_the_answer():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Run the tests")
    await _settle()
    live.drop()
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech() == []
    assert dispatcher.claimed == ["turn-1"]

    live.emit("session_reconnected")
    assert live.speech(None) == ["All tests pass. The build is green."]
    (note,) = _redelivery_notes(live)
    assert "present it as the answer" in note
    (briefing,) = [t for t in live.of("thinking", None) if "re-established" in t]
    assert "still working" not in briefing
    # Spoken by the new session: a later reconnect does not repeat it.
    await asyncio.sleep(live_delegation.DELIVERY_CONFIRM_MIN_SECONDS + 0.05)
    bridge.on_agent_state_changed("listening", "speaking")
    live.drop()
    live.emit("session_reconnected")
    assert live.speech(None) == ["All tests pass. The build is green."]
    assert len(_redelivery_notes(live)) == 1
    await bridge.aclose()


async def test_commentary_spoken_before_the_drop_is_not_redelivered():
    clock = {"now": 100.0}
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        clock=lambda: clock["now"]
    )
    live.delegate("d1", "Run the tests")
    await _settle()
    dispatcher.progress("turn-1", _progress_snapshot("Tests are running. Six passed."))
    assert live.speech("d1") == ["Tests are running. Six passed."]
    clock["now"] += 1.0
    bridge.on_agent_state_changed("listening", "speaking")
    bridge.on_agent_state_changed("speaking", "listening")
    clock["now"] += 5.0

    live.drop()
    live.emit("session_reconnected")
    assert live.speech(None) == []
    assert _redelivery_notes(live) == []
    await bridge.aclose()


async def test_bound_commentary_the_model_never_spoke_is_redelivered_once():
    """Sent to the old session's delegation just before it died."""
    clock = {"now": 100.0}
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        clock=lambda: clock["now"]
    )
    live.delegate("d1", "Run the tests")
    await _settle()
    # The acknowledgement already under way does not count as speaking this.
    bridge.on_agent_state_changed("listening", "speaking")
    dispatcher.progress("turn-1", _progress_snapshot("Tests are running. Six passed."))
    clock["now"] += 0.1
    bridge.on_agent_state_changed("speaking", "listening")
    bridge.on_agent_state_changed("listening", "speaking")
    clock["now"] += 0.4

    live.drop()
    live.emit("session_reconnected")
    assert live.speech(None) == ["Tests are running. Six passed."]
    assert len(_redelivery_notes(live)) == 1
    await bridge.aclose()


async def test_old_bound_commentary_is_not_redelivered_without_a_speaking_cue():
    clock = {"now": 100.0}
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        clock=lambda: clock["now"]
    )
    live.delegate("d1", "Run the tests")
    await _settle()
    dispatcher.progress("turn-1", _progress_snapshot("Tests are running. Six passed."))
    clock["now"] += live_delegation.REDELIVERY_MAX_AGE_SECONDS + 1
    live.drop()
    live.emit("session_reconnected")
    assert live.speech(None) == []
    await bridge.aclose()


async def test_a_held_utterance_and_a_redelivered_final_do_not_interleave():
    """The caller was mid-sentence at the drop and the turn finished meanwhile.

    The plugin closes the caller's speech before ``session_reconnected``, so
    the new words are held when the reconnect arrives; the finished answer
    goes out first, and the held words start their own turn after it instead
    of superseding the answer away.
    """
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(settle=5.0)
    live.delegate("d1", "Run the tests")
    live.final("Run the tests")  # covered by the delegation
    await _settle()
    live.drop()
    dispatcher.result_gate.set()
    await _settle()
    live.final("And then the linter")
    assert bridge._held is not None

    live.emit("session_reconnected")
    assert live.speech(None) == ["All tests pass. The build is green."]
    assert bridge._held is not None
    assert len(dispatcher.prompts) == 1

    bridge._flush_held()
    await _settle()
    assert len(dispatcher.prompts) == 2
    assert dispatcher.prompts[1][0].endswith(_voice("And then the linter"))
    answer_at = next(
        i
        for i, (kind, text, _) in enumerate(live.appends)
        if kind == "instructions" and "All tests pass. The build is green." in text
    )
    taken_at = next(
        i
        for i, (kind, text, _) in enumerate(live.appends)
        if kind == "thinking" and "And then the linter" in text
    )
    assert answer_at < taken_at
    assert live.speech(None) == ["All tests pass. The build is green."]
    await bridge.aclose()


async def test_a_recoverable_server_error_does_not_hold_commentary():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Run the tests")
    await _settle()
    live.server_error()
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == ["All tests pass. The build is green."]
    await bridge.aclose()


@pytest.mark.parametrize("held", [False, True])
async def test_reconnect_retains_every_unheard_streamed_chunk(held):
    clock = {"now": 100.0}
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        clock=lambda: clock["now"]
    )
    live.delegate("d1", "Run the tests")
    await _settle()
    if held:
        live.drop()
    sentences = [f"Test number {number} passed." for number in range(12)]
    for count in range(1, len(sentences) + 1):
        dispatcher.progress("turn-1", _progress_snapshot(" ".join(sentences[:count])))
    dispatcher.result = {
        "_livekit_speech_text": " ".join(sentences),
        "_livekit_turn_id": "turn-1",
        "status": "completed",
        "progress": {},
    }
    dispatcher.result_gate.set()
    await _settle()
    if not held:
        live.drop()
    live.emit("session_reconnected")
    assert live.speech(None) == sentences
    await bridge.aclose()


async def test_buffered_speech_during_the_gap_does_not_confirm_old_appends():
    clock = {"now": 100.0}
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        clock=lambda: clock["now"]
    )
    live.delegate("d1", "Run the tests")
    await _settle()
    dispatcher.result_gate.set()
    await _settle()
    live.drop()
    clock["now"] += 1.0
    bridge.on_agent_state_changed("listening", "speaking")
    live.emit("session_reconnected")
    assert live.speech(None) == ["All tests pass. The build is green."]
    await bridge.aclose()


async def test_steering_during_the_gap_preserves_the_shared_turns_unheard_prefix():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Run the tests")
    live.final("Run the tests")
    await _settle()
    live.drop()
    dispatcher.progress("turn-1", _progress_snapshot("Tests passed."))
    live.final("Also run the linter")
    await _settle()
    dispatcher.result = {
        "_livekit_speech_text": "Tests passed. Linter passed.",
        "_livekit_turn_id": "turn-1",
        "status": "completed",
        "progress": {},
    }
    dispatcher.result_gate.set()
    await _settle()
    live.emit("session_reconnected")
    assert live.speech(None) == ["Tests passed.", "Linter passed."]
    assert len(_redelivery_notes(live)) == 1
    await bridge.aclose()


@pytest.mark.parametrize("drop_seen", [False, True])
async def test_unbound_delivery_reconnect_requires_an_observed_drop(drop_seen):
    clock = {"now": 100.0}
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        clock=lambda: clock["now"]
    )
    live.final("Run the tests")
    await _settle()
    dispatcher.result_gate.set()
    await _settle()
    clock["now"] += 1.0
    if drop_seen:
        live.drop()
    live.emit("session_reconnected")
    assert live.speech(None) == ["All tests pass. The build is green."] * (
        2 if drop_seen else 1
    )
    if drop_seen:
        clock["now"] += 1.0
        live.drop()
        live.emit("session_reconnected")
        assert len(live.speech(None)) == 3
    await bridge.aclose()


@pytest.mark.parametrize("stale_reason", ["completed_turn_superseded", "route_changed"])
async def test_reconnect_does_not_replay_stale_held_answers(stale_reason):
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Run the tests")
    live.final("Run the tests")
    await _settle()
    live.drop()
    dispatcher.result_gate.set()
    await _settle()
    if stale_reason == "route_changed":
        router.transfer(FakeVoiceClient(thread_id="other-thread"))
    else:
        dispatcher.result_gate.clear()
        live.final("Now inspect the build")
        await _settle()
    live.emit("session_reconnected")
    assert live.speech() == []
    await bridge.aclose()


async def test_held_answer_survives_a_long_outage_and_claims_orphan_delivery():
    clock = {"now": 100.0}
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        clock=lambda: clock["now"]
    )
    live.delegate("d1", "Run the tests")
    await _settle()
    live.drop()
    dispatcher.result_gate.set()
    await _settle()
    bridge.deliver_orphaned_result(
        dispatcher, "turn-1", "All tests pass. The build is green."
    )
    assert live.speech() == []
    clock["now"] += 120.0
    live.emit("session_reconnected")
    assert live.speech(None) == ["All tests pass. The build is green."]
    await bridge.aclose()


async def test_appends_during_the_gap_are_not_bound_to_the_dead_session():
    """A turn steered during the gap inherits the dead delegation; its thinking
    must not be queued for that id (the new session would reject it)."""
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Run the tests")
    live.final("Run the tests")
    await _settle()
    bound_before = len(live.of("thinking", "d1"))
    live.drop()
    live.final("Also run the linter")
    await _settle()
    assert len(dispatcher.prompts) == 2
    assert bridge._newest_entry_for(dispatcher).delegation_id == "d1"
    assert len(live.of("thinking", "d1")) == bound_before
    assert any("Also run the linter" in t for t in live.of("thinking", None))

    live.emit("session_reconnected")
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == []
    assert live.speech(None) == ["All tests pass. The build is green."]
    await bridge.aclose()


async def test_pending_approval_is_spoken_once_and_retained_as_instructions():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Deploy it")
    await _settle()
    snapshot = {"status": "waiting", "pendingRequests": [{"id": "approval-1"}]}
    dispatcher.progress("turn-1", snapshot)
    dispatcher.progress("turn-1", snapshot)
    assert live.speech("d1") == [LIVE_APPROVAL_PENDING_COMMENTARY]
    assert len(live.of("instructions", "d1")) == 2
    assert "Approvals" in live.of("instructions", "d1")[-1]
    dispatcher.result_gate.set()
    await _settle()
    await bridge.aclose()


async def test_empty_answer_closes_the_delegation_with_a_short_commentary():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    dispatcher.result = {
        "_livekit_speech_text": "",
        "_livekit_turn_id": "turn-1",
        "status": "completed",
        "progress": {},
    }
    live.delegate("d1", "Do the thing")
    await _settle()
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == [LIVE_EMPTY_ANSWER_COMMENTARY]
    await bridge.aclose()


async def test_delegation_without_new_speech_never_tells_the_model_to_answer():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "")
    await _settle()
    assert dispatcher.prompts == []
    (thinking,) = live.of("thinking", "d1")
    assert "Do not answer from your own knowledge" in thinking
    assert "answer from the conversation" not in thinking
    assert lifecycle == []
    await bridge.aclose()


# --- the agent is always the brain: every utterance reaches the thread --------


def _answer(dispatcher, text, turn_id="turn-1"):
    dispatcher.result = {
        "_livekit_speech_text": text,
        "_livekit_turn_id": turn_id,
        "status": "completed",
        "progress": {},
    }


async def test_desktop_question_reaches_the_agent_without_any_delegation():
    """Regression: GPT-Live answered "what's on my desktop" from its own knowledge.

    The model emits no ``delegation_created`` at all; the closed utterance
    still goes to the active thread and the thread's answer is spoken.
    """
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    _answer(dispatcher, "Your desktop has two folders and a screenshot.")

    live.partial("what's on my")
    await _settle()
    assert dispatcher.prompts == [], "partial transcripts must not start turns"

    live.final("what's on my desktop", item_id="speech_1")
    # The same final delivered twice (same item id) is one utterance.
    live.final("what's on my desktop", item_id="speech_1")
    await _settle()

    assert len(dispatcher.prompts) == 1
    prompt, instructions = dispatcher.prompts[0]
    assert prompt.endswith(wrap_voice_prompt("what's on my desktop"))
    assert instructions == "Direct route guidance."
    (thinking,) = live.of("thinking", None)
    assert "what's on my desktop" in thinking
    assert "do not answer it yourself" in thinking
    assert live.speech() == []
    assert ("utterance_accepted", "live-utt-1") in lifecycle

    dispatcher.result_gate.set()
    await _settle()
    assert live.speech(None) == ["Your desktop has two folders and a screenshot."]
    assert dispatcher.claimed == ["turn-1"]
    await bridge.aclose()


async def test_delegation_after_the_final_binds_to_the_running_turn():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.final("Check whether the build passes")
    await _settle()
    assert len(dispatcher.prompts) == 1

    # The model delegates the same, already closed utterance.
    live.delegate("d1", "")
    await _settle()
    assert len(dispatcher.prompts) == 1, "exactly one agent turn"
    (bound,) = live.of("thinking", "d1")
    assert "Check whether the build passes" in bound

    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == ["All tests pass. The build is green."]
    assert live.speech(None) == []
    await bridge.aclose()


async def test_delegation_before_the_final_is_not_sent_twice():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    # The model delegates while the caller's utterance is still open...
    live.delegate("d1", "Check whether the build passes")
    await _settle()
    # ... and the plugin then closes the same utterance.
    live.final("Check whether the build passes.")
    await _settle()
    assert len(dispatcher.prompts) == 1, "exactly one agent turn"

    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == ["All tests pass. The build is green."]
    assert live.speech(None) == []
    await bridge.aclose()


async def test_words_added_after_the_delegation_steer_the_same_delegation():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Check the build")
    await _settle()
    live.final("Check the build and run the linter")
    await _settle()
    assert len(dispatcher.prompts) == 2
    assert dispatcher.prompts[1][0].endswith(
        wrap_voice_prompt("Check the build and run the linter")
    )

    dispatcher.result_gate.set()
    await _settle()
    # The steered turn's merged answer is spoken once, still answering d1.
    assert live.speech("d1") == ["All tests pass. The build is green."]
    assert live.speech(None) == []
    await bridge.aclose()


async def test_trailing_small_talk_after_the_delegation_is_covered():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Check the build")
    await _settle()
    live.final("Check the build, okay thanks.")
    await _settle()
    assert len(dispatcher.prompts) == 1
    dispatcher.result_gate.set()
    await _settle()
    await bridge.aclose()


async def test_two_utterances_in_a_row_steer_and_only_the_newest_speaks():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.final("Check the build")
    await _settle()
    live.final("And also run the linter")
    await _settle()
    assert len(dispatcher.prompts) == 2
    assert dispatcher.prompts[0][0].endswith(wrap_voice_prompt("Check the build"))
    assert dispatcher.prompts[1][0].endswith(
        wrap_voice_prompt("And also run the linter")
    )
    assert dispatcher.replaces == [False, False]
    # A late delegation binds to the newest utterance.
    live.delegate("d1", "")
    await _settle()

    dispatcher.result_gate.set()
    await _settle()
    # The superseded first result is dropped; the merged answer speaks once.
    assert live.speech() == ["All tests pass. The build is green."]
    assert live.speech("d1") == ["All tests pass. The build is green."]
    assert dispatcher.claimed == ["turn-1"]
    first = next(r for r in ledger._records.values() if r.message_id == "live-utt-1")
    assert first.status == "cancelled"
    await bridge.aclose()


async def test_trivial_utterances_stay_off_the_agent_unless_the_model_delegates():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.final("Thanks!")
    live.final("Mm-hmm.")
    await _settle()
    assert dispatcher.prompts == []
    assert live.appends == []

    # An answer the model judged substantive ("Okay." to an agent's question)
    # still reaches the agent when the model delegates it.
    live.final("Okay.")
    live.delegate("d1", "")
    await _settle()
    assert len(dispatcher.prompts) == 1
    assert dispatcher.prompts[0][0].endswith(wrap_voice_prompt("Okay."))
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == ["All tests pass. The build is green."]
    await bridge.aclose()


async def test_trivial_open_utterance_delegation_binds_to_the_running_request():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.final("Check the build")
    await _settle()
    live.delegate("d1", "um")
    await _settle()
    assert len(dispatcher.prompts) == 1
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == ["All tests pass. The build is green."]
    await bridge.aclose()


async def test_late_delegation_for_an_answered_utterance_is_not_rerun():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.final("Check the build")
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech(None) == ["All tests pass. The build is green."]

    live.delegate("d1", "")
    await _settle()
    assert len(dispatcher.prompts) == 1
    (thinking,) = live.of("thinking", "d1")
    assert "already answered" in thinking
    assert live.speech("d1") == []
    await bridge.aclose()


async def test_stale_turn_is_not_bound_to_a_much_later_delegation():
    clock = {"now": 100.0}
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        clock=lambda: clock["now"]
    )
    live.final("Check the build")
    await _settle()
    clock["now"] += live_delegation.DELEGATION_BIND_WINDOW_SECONDS + 1
    live.delegate("d1", "")
    await _settle()
    assert len(dispatcher.prompts) == 1
    assert "Do not answer from your own knowledge" in live.of("thinking", "d1")[0]
    dispatcher.result_gate.set()
    await _settle()
    await bridge.aclose()


async def test_route_change_sends_the_next_final_to_the_new_agent():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Check the build")
    await _settle()
    other = FakeVoiceClient(thread_id="thread-2")
    router.transfer(other)
    bridge.notify_route_changed(action="transfer_to_thread", agent_label="Lucy")
    live.final("Check the build")
    await _settle()
    assert len(dispatcher.prompts) == 1
    assert len(other.prompts) == 1
    dispatcher.result_gate.set()
    other.result_gate.set()
    await _settle()
    await bridge.aclose()


async def test_exit_command_delegated_first_is_not_sent_when_its_final_arrives():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    other = FakeVoiceClient(thread_id="thread-2")
    router.transfer(other)
    live.delegate("d1", "Exit to dispatch")
    await _settle()
    assert router.is_dispatcher_active
    live.final("Exit to dispatch.")
    await _settle()
    assert dispatcher.prompts == [] and other.prompts == []
    assert live.speech("d1") == [BACK_TO_DISPATCH_COMMENTARY]
    await bridge.aclose()


async def test_exit_command_marker_does_not_swallow_the_next_request():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    other = FakeVoiceClient(thread_id="thread-2")
    router.transfer(other)
    live.final("Exit to dispatch.")
    live.final("What's on my desktop?")
    live.delegate("d1", "")
    await _settle()
    assert len(dispatcher.prompts) == 1
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == ["All tests pass. The build is green."]
    await bridge.aclose()


async def test_bridge_subscribes_to_closed_utterances_and_delegations():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    assert len(live._handlers["input_audio_transcription_completed"]) == 1
    assert len(live._handlers["delegation_created"]) == 1
    assert len(live._handlers["session_reconnected"]) == 1
    assert len(live._handlers["error"]) == 1
    await bridge.aclose()
    assert live._handlers["input_audio_transcription_completed"] == []
    assert live._handlers["delegation_created"] == []
    assert live._handlers["session_reconnected"] == []
    assert live._handlers["error"] == []


async def test_character_suspension_preserves_input_and_holds_results_until_restore():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    bridge.suspend_session()
    bridge.suspend_session()
    live.final("Check the build", item_id="caller")
    await _settle()
    assert len(dispatcher.prompts) == 1
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech() == []
    replacement = FakeGPTLiveSession()
    bridge.attach(replacement)
    bridge.on_character_session_started()
    assert replacement.speech(None) == ["All tests pass. The build is green."]
    assert all("connection dropped" not in text for text in replacement.of("thinking"))
    assert all("Do not greet" not in text for text in replacement.of("thinking"))
    assert live._handlers["input_audio_transcription_completed"] == []
    await bridge.aclose()


async def test_planned_handoff_without_work_supplies_no_reconnect_or_greeting():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    bridge.suspend_session()
    replacement = FakeGPTLiveSession()
    bridge.attach(replacement)
    bridge.on_character_session_started()
    assert replacement.appends == []
    # A later real socket loss still uses the recovery briefing.
    replacement.drop()
    replacement.emit("session_reconnected")
    assert any("re-established" in text for text in replacement.of("thinking"))
    await bridge.aclose()


async def test_character_suspension_discards_input_from_the_previous_route():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    other = FakeVoiceClient(thread_id="other")
    router.transfer(other)
    bridge.suspend_session()
    live.final("Words from the previous route", item_id="caller")
    await _settle()
    assert dispatcher.prompts == []
    assert other.prompts == []
    await bridge.aclose()


async def test_return_command_survives_character_handoff_once():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    other = FakeVoiceClient(thread_id="other")
    router.transfer(other)
    bridge.suspend_session()
    # The previous model finishes this newer caller command while the
    # target's immutable voice session is still connecting.
    live.final("Return to Dispatcher.", item_id="return-during-handoff")
    live.final("Return to Dispatcher.", item_id="return-during-handoff")
    await _settle()
    assert router.is_dispatcher_active
    assert router.exits == 1
    assert dispatcher.prompts == [] and other.prompts == []
    await bridge.aclose()


async def test_return_while_dispatcher_active_cancels_pending_transfer_without_turn():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    router.has_pending_transfer = True
    live.final("Return to Dispatcher.")
    await _settle()
    assert router.exits == 1
    assert dispatcher.prompts == []
    await bridge.aclose()


# --- overlapping delegations / steering ----------------------------------------


async def test_overlapping_delegations_let_only_the_newest_speak_once():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Check the build")
    await _settle()
    # The user speaks again while the turn runs: run_turn steers the same
    # turn, so both calls return the same result.
    live.delegate("d2", "and also run the linter")
    await _settle()
    assert len(dispatcher.prompts) == 2
    assert ("utterance_accepted", "live-d1") in lifecycle
    assert ("utterance_accepted", "live-d2") in lifecycle

    dispatcher.result_gate.set()
    await _settle()

    assert live.speech("d1") == []
    assert live.speech("d2") == ["All tests pass. The build is green."]
    assert dispatcher.claimed == ["turn-1"]
    await bridge.aclose()


async def test_result_for_a_superseded_route_is_dropped():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Check the build")
    await _settle()
    other = FakeVoiceClient(thread_id="thread-2")
    router.transfer(other)
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == []
    await bridge.aclose()


# --- route changes and spoken commands -----------------------------------------


async def test_route_change_appends_thinking_and_a_spoken_handoff_naming_the_agent():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    bridge.notify_route_changed(action="transfer_to_thread", agent_label="Lucy")
    thinking = live.of("thinking", None)
    assert thinking and "Lucy" in thinking[0]
    assert "Direct route guidance." in thinking[0]
    assert live.speech(None) == ["You are now talking to Lucy."]
    assert bridge.active_agent_label == "Lucy"

    bridge.notify_route_changed(action="exit_to_dispatch", agent_label=None)
    assert live.speech(None)[-1] == BACK_TO_DISPATCH_COMMENTARY
    assert bridge.active_agent_label == live_delegation.DISPATCHER_AGENT_LABEL
    await bridge.aclose()


async def test_exit_to_dispatch_spoken_on_the_transcript_switches_route_without_a_turn():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    other = FakeVoiceClient(thread_id="thread-2")
    router.transfer(other)

    bridge.on_user_transcript("Exit to dispatch.", is_final=True)
    assert router.is_dispatcher_active
    assert live.speech(None) == [BACK_TO_DISPATCH_COMMENTARY]

    # The model delegates the same utterance a moment later: it is answered
    # as a command receipt, never sent to the dispatcher as a prompt.
    live.delegate("d1", "Exit to dispatch.")
    await _settle()
    assert dispatcher.prompts == []
    assert other.prompts == []
    assert live.speech("d1") == [BACK_TO_DISPATCH_COMMENTARY]
    await bridge.aclose()


async def test_exit_to_dispatch_delegated_first_switches_route():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    other = FakeVoiceClient(thread_id="thread-2")
    router.transfer(other)
    live.delegate("d1", "to dispatch")
    await _settle()
    assert router.is_dispatcher_active
    assert live.speech("d1") == [BACK_TO_DISPATCH_COMMENTARY]
    assert other.prompts == [] and dispatcher.prompts == []
    await bridge.aclose()


async def test_exit_to_dispatch_while_dispatcher_active_is_a_normal_prompt():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "exit to dispatch")
    await _settle()
    assert len(dispatcher.prompts) == 1
    dispatcher.result_gate.set()
    await _settle()
    await bridge.aclose()


# --- errors -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("busy", "expected"),
    [
        (True, LIVE_BACKEND_BUSY_COMMENTARY),
        (False, LIVE_BACKEND_UNRESPONSIVE_COMMENTARY),
    ],
)
async def test_turn_errors_become_immediate_commentary(busy, expected):
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    dispatcher.busy = busy
    dispatcher.result = RuntimeError("app-server down")
    live.delegate("d1", "Check the build")
    await _settle()
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == [expected]
    record = next(r for r in ledger._records.values() if r.message_id == "live-d1")
    assert record.status == "cancelled"
    await bridge.aclose()


# --- announcer ------------------------------------------------------------------


async def test_greeting_is_one_spoken_instruction_not_another_commentary_answer():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    try:
        bridge.greet("Hi, I'm Jacqueline.")
        assert len(live.of("instructions", None)) == 1
        assert live.of("instructions", None)[0].endswith('"Hi, I\'m Jacqueline."')
        assert live.speech() == []
        assert bridge.speech_gate.authorized
        bridge.on_user_state_changed("listening", "speaking")
        assert not bridge.speech_gate.authorized
    finally:
        await bridge.aclose()


async def test_user_say_announcements_become_session_wide_commentary():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    bridge.announce("Finished the report.", agent_name="Lucy")
    bridge.announce("Back to dispatch.")
    assert live.speech(None) == [
        "Lucy: Finished the report.",
        "Back to dispatch.",
    ]
    await bridge.aclose()


async def test_orphaned_results_are_spoken_once():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    bridge.deliver_orphaned_result(dispatcher, "turn-9", "The deploy finished.")
    bridge.deliver_orphaned_result(dispatcher, "turn-9", "The deploy finished.")
    assert live.speech(None) == ["The deploy finished."]
    await bridge.aclose()


@pytest.mark.parametrize("gap", ["suspend", "disconnect"])
async def test_orphaned_result_is_retained_until_the_conversation_session_returns(gap):
    clock = {"now": 100.0}
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        clock=lambda: clock["now"]
    )
    if gap == "suspend":
        bridge.suspend_session()
    else:
        live.drop()
    bridge.deliver_orphaned_result(dispatcher, "turn-9", "The deploy finished.")
    bridge.deliver_orphaned_result(dispatcher, "turn-9", "The deploy finished.")
    assert dispatcher.claimed == ["turn-9"]
    assert live.speech() == []
    clock["now"] += 120
    replacement = FakeGPTLiveSession()
    bridge.attach(replacement)
    bridge.on_session_reconnected()
    assert replacement.speech(None) == ["The deploy finished."]
    clock["now"] += 1
    bridge.on_agent_state_changed("listening", "speaking")
    bridge.suspend_session()
    next_session = FakeGPTLiveSession()
    bridge.attach(next_session)
    bridge.on_session_reconnected()
    assert next_session.speech() == []
    await bridge.aclose()


@pytest.mark.parametrize("return_to_route", [False, True])
async def test_held_orphaned_result_never_replays_after_route_changes(return_to_route):
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    bridge.suspend_session()
    bridge.deliver_orphaned_result(dispatcher, "turn-9", "Old route answer.")
    router.transfer(FakeVoiceClient(thread_id="other-thread"))
    if return_to_route:
        router.exit_to_dispatch()
    replacement = FakeGPTLiveSession()
    bridge.attach(replacement)
    bridge.on_session_reconnected()
    bridge.deliver_orphaned_result(dispatcher, "turn-9", "Old route answer.")
    assert replacement.speech() == []
    await bridge.aclose()


async def test_orphaned_result_does_not_take_over_a_callers_pending_delegation():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("current", "Run the tests")
    await _settle()
    current = bridge._newest_entry_for(dispatcher)
    bridge.suspend_session()
    bridge.deliver_orphaned_result(dispatcher, "orphaned-turn", "Prior work finished.")
    assert bridge._newest_entry_for(dispatcher) is current
    replacement = FakeGPTLiveSession()
    bridge.attach(replacement)
    bridge.on_session_reconnected()
    replacement.delegate("new-session-delegation")
    assert current.delegation_id == "new-session-delegation"
    dispatcher.result_gate.set()
    await _settle()
    assert replacement.speech(None) == ["Prior work finished."]
    assert replacement.speech("new-session-delegation") == [
        "All tests pass. The build is green."
    ]
    await bridge.aclose()


async def test_orphaned_result_reuses_its_matching_turn_delivery():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("current", "Run the tests")
    await _settle()
    dispatcher.progress("turn-1", {})
    entry = bridge._newest_entry_for(dispatcher)
    bridge.suspend_session()
    bridge.deliver_orphaned_result(
        dispatcher, "turn-1", "All tests pass. The build is green."
    )
    assert list(bridge._entries.values()) == [entry]
    dispatcher.result_gate.set()
    await _settle()
    replacement = FakeGPTLiveSession()
    bridge.attach(replacement)
    bridge.on_session_reconnected()
    assert replacement.speech(None) == ["All tests pass. The build is green."]
    assert dispatcher.claimed == ["turn-1"]
    await bridge.aclose()


async def test_orphaned_result_is_not_a_new_caller_utterance():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    bridge.deliver_orphaned_result(dispatcher, "turn-9", "The deploy finished.")
    live.delegate("no-new-caller-input")
    assert all(entry.delegation_id is None for entry in bridge._entries.values())
    assert dispatcher.prompts == []
    await bridge.aclose()


# --- lifecycle packets --------------------------------------------------------------


async def test_lifecycle_brackets_model_audio_and_never_emits_mute_or_unmute():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Check the build")
    await _settle()
    # The model acknowledges while the turn runs.
    bridge.on_agent_state_changed("listening", "speaking")
    bridge.on_agent_state_changed("speaking", "listening")
    dispatcher.result_gate.set()
    await _settle()
    # ... and later speaks the answer.
    bridge.on_agent_state_changed("thinking", "speaking")
    bridge.on_agent_state_changed("speaking", "listening")
    # Every mute-related path the pipeline uses is a no-op in live mode.
    ledger.notify_user_state(new_state="speaking")
    ledger.notify_user_state(new_state="listening", old_state="speaking")
    ledger.notify_vad_gap(2.0)
    await asyncio.sleep(0)

    events = [event for event, _ in lifecycle]
    assert events[:3] == [
        "utterance_accepted",
        "agent_audio_started",
        "agent_audio_finished",
    ]
    assert events.count("agent_audio_started") == 2
    assert events.count("agent_audio_finished") == 2
    assert "safe_to_mute_user" not in events
    assert "safe_to_unmute" not in events
    assert "mute_keepalive" not in events
    await bridge.aclose()


def test_live_mode_ledger_suppresses_every_mute_release_path():
    sink: list[str] = []
    ledger = VoiceDeliveryLedger(
        route_snapshot=lambda: VoiceRouteSnapshot(0, "t", None, None, "dispatcher"),
        live_mode=True,
    )
    ledger.set_lifecycle_sink(lambda event, record, reason: sink.append(event))
    record = ledger.accept_utterance(message_id="live-x", prompt="hello")
    ledger._emit_lifecycle("safe_to_mute_user", record)
    ledger._emit_lifecycle("safe_to_unmute", record)
    ledger._emit_lifecycle("mute_keepalive", record)
    ledger.mark_live_audio_started(record)
    ledger.mark_live_audio_finished(record)
    assert sink == ["utterance_accepted", "agent_audio_started", "agent_audio_finished"]
    assert record.status == "audio_delivered" and record.delivered


def test_pipeline_ledger_is_unchanged_by_the_live_mode_flag():
    sink: list[str] = []
    ledger = VoiceDeliveryLedger(
        route_snapshot=lambda: VoiceRouteSnapshot(0, "t", None, None, "dispatcher")
    )
    ledger.set_lifecycle_sink(lambda event, record, reason: sink.append(event))
    record = ledger.accept_utterance(message_id="m", prompt="hello")
    ledger._emit_lifecycle("safe_to_mute_user", record)
    assert ledger.live_mode is False
    assert sink == ["utterance_accepted", "safe_to_mute_user"]
    ledger._cancel_mute_keepalive_task()


# --- BUG 18: screen context and fragment settling ----------------------------


class _Focus:
    def __init__(self, focus):
        self.focus = focus

    def current(self):
        return self.focus


LIGHTHOUSE = FocusedThread(
    thread_id="s_4edd576829854b68b142367d698474f2",
    name="lighthouse-738",
    directory="/data/workspace/tic-tac-toe",
)


async def test_dispatcher_prompt_names_the_thread_open_on_the_phone():
    """Regression for BUG 18 (2026-10-09): the caller, looking at a project
    thread, said "this thread"; the dispatcher got no hint which one."""
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    router.focused_thread_tracker = _Focus(LIGHTHOUSE)
    live.final("Subtract 38 from the multiplication result in this thread.")
    await _settle()
    (prompt, _instructions) = dispatcher.prompts[0]
    assert prompt.startswith("[Openbase system note: the caller has the thread")
    assert LIGHTHOUSE.thread_id in prompt
    assert '"lighthouse-738"' in prompt
    assert "super_agents_steer" in prompt
    assert prompt.endswith(
        wrap_voice_prompt("Subtract 38 from the multiplication result in this thread.")
    )
    await bridge.aclose()


async def test_no_screen_note_without_an_open_project_thread():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    router.focused_thread_tracker = _Focus(None)
    live.final("Check the build")
    await _settle()
    assert "screen" not in dispatcher.prompts[0][0]
    assert "the caller has the thread" not in dispatcher.prompts[0][0]
    await bridge.aclose()


async def test_no_screen_note_once_the_call_is_transferred_into_a_thread():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    router.focused_thread_tracker = _Focus(LIGHTHOUSE)
    target = FakeVoiceClient(thread_id=LIGHTHOUSE.thread_id)
    router.transfer(target)
    live.final("What is the result now?")
    await _settle()
    assert target.prompts
    assert "the caller has the thread" not in target.prompts[0][0]
    await bridge.aclose()


async def test_return_keeps_spoken_agent_context_separate_from_viewed_thread():
    bridge, live, router, dispatcher, _, _ = _make_bridge()
    marian = FakeVoiceClient(thread_id="marian-thread")
    marian.result = {
        "_livekit_turn_id": "marian-answer",
        "_livekit_speech_text": "I am Marian. Our test project is Seaglass. 42.",
    }
    router.transfer(marian)
    bridge.notify_route_changed(action="transfer_to_thread", agent_label="Marian")
    live.final("Remember our test project is Seaglass. What is seven times six?")
    await _settle()
    marian.result_gate.set()
    await _settle()
    router.focused_thread_tracker = _Focus(FocusedThread("theo-thread", "Theo"))
    # Background announcements and reconnects must not become call transfers.
    bridge.announce("Unrelated project is Orchard", agent_name="Gemma")
    live.drop()
    live.emit("session_reconnected")
    live.final("Back to dispatch")
    await _settle()
    live.final("Which agent was I just speaking with and what is our project named?")
    await _settle()
    prompt = dispatcher.prompts[-1][0]
    assert "Seaglass" in prompt
    assert '"agent": "Marian"' in prompt
    assert '"thread_id": "marian-thread"' in prompt
    assert '"role": "dispatcher"' in prompt
    assert "theo-thread" in prompt  # Screen routing still available explicitly.
    assert "not the previously spoken agent" in prompt
    assert "Orchard" not in prompt
    await bridge.aclose()


async def test_late_old_route_result_does_not_enter_return_context():
    bridge, live, router, dispatcher, _, _ = _make_bridge()
    marian = FakeVoiceClient(thread_id="marian-thread")
    marian.result["_livekit_speech_text"] = "Stale secret project answer."
    router.transfer(marian)
    bridge.notify_route_changed(action="transfer_to_thread", agent_label="Marian")
    live.final("Check the project")
    await _settle()
    live.final("Back to dispatch")
    await _settle()
    marian.result_gate.set()
    await _settle()
    live.final("Who was I speaking with?")
    await _settle()
    prompt = dispatcher.prompts[-1][0]
    assert '"agent": "Marian"' in prompt
    assert "Stale secret project answer" not in prompt
    await bridge.aclose()


async def test_a_sentence_still_being_transcribed_joins_the_held_one():
    """Regression for BUG 18's split: "... in this thread" closed first and
    ". Answer just the number" arrived later as its own final, so the
    dispatcher got two turns. The caller's voice (session VAD) stopped after
    both sentences; the hold waits the transcript lag past that point."""
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        settle=0.02, hold_max=2.0, lag=0.3
    )
    bridge.on_user_state_changed("listening", "speaking")
    bridge.on_user_state_changed("speaking", "listening")
    live.final("Subtract 38 from the multiplication result in this thread")
    await asyncio.sleep(0.15)
    assert dispatcher.prompts == []
    live.final(". Answer just the number")
    await asyncio.sleep(0.45)
    assert len(dispatcher.prompts) == 1
    assert dispatcher.prompts[0][0].endswith(
        wrap_voice_prompt(
            "Subtract 38 from the multiplication result in this thread. Answer just the number"
        )
    )
    await bridge.aclose()


async def test_the_hold_waits_while_the_caller_is_still_speaking():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        settle=0.02, hold_max=2.0, lag=0.1
    )
    live.final("Subtract 38 from the multiplication result in this thread")
    bridge.on_user_state_changed("listening", "speaking")
    await asyncio.sleep(0.2)
    assert dispatcher.prompts == []
    live.final("Answer just the number")
    bridge.on_user_state_changed("speaking", "listening")
    await asyncio.sleep(0.3)
    assert len(dispatcher.prompts) == 1
    assert dispatcher.prompts[0][0].endswith(
        wrap_voice_prompt(
            "Subtract 38 from the multiplication result in this thread Answer just the number"
        )
    )
    await bridge.aclose()


@pytest.mark.parametrize("delegated", [False, True])
async def test_long_spoken_request_outlives_hold_cap_without_executing_its_prefix(
    delegated,
):
    # Android field regression: a 19-second request was dispatched twice at
    # the six-second cap, before its completion wording and constraints arrived.
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        settle=0.02, hold_max=0.06, lag=0.1
    )
    try:
        bridge.on_user_state_changed("listening", "speaking")
        live.final("Ask Gemma to read the README", item_id="part-1")
        if delegated:
            live.delegate("d1", "")
        await asyncio.sleep(0.15)
        assert dispatcher.prompts == []
        live.final("and announce completion", item_id="part-2")
        bridge.on_user_state_changed("speaking", "listening")
        await asyncio.sleep(0.04)
        assert dispatcher.prompts == []  # The expired cap must not erase STT lag.
        live.final("without changing files or starting agents", item_id="part-3")
        await asyncio.sleep(0.15)
        assert len(dispatcher.prompts) == 1
        assert dispatcher.prompts[0][0].endswith(
            wrap_voice_prompt(
                "Ask Gemma to read the README and announce completion "
                "without changing files or starting agents"
            )
        )
    finally:
        await bridge.aclose()


async def test_a_delegation_mid_request_binds_without_cutting_the_hold_short():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        settle=0.1, hold_max=2.0
    )
    live.final("Subtract 38 from the multiplication result in this thread")
    # The caller is still speaking the next sentence when the model delegates.
    bridge.on_user_state_changed("listening", "speaking")
    live.delegate("d1", "")
    await _settle()
    assert dispatcher.prompts == []
    bridge.on_user_state_changed("speaking", "listening")
    live.final(". Answer just the number")
    await asyncio.sleep(0.25)
    assert len(dispatcher.prompts) == 1
    assert dispatcher.prompts[0][0].endswith(
        wrap_voice_prompt(
            "Subtract 38 from the multiplication result in this thread. Answer just the number"
        )
    )
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == ["All tests pass. The build is green."]
    await bridge.aclose()


async def test_words_after_a_silent_turn_started_carry_the_whole_request():
    """When the rest of the request comes after the hold (backends that cannot
    steer queue it as a separate turn), the follow-up must stand on its own."""
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.final("Subtract 38 from the multiplication result in this thread")
    await _settle()
    live.final(". Answer just the number")
    await _settle()
    assert len(dispatcher.prompts) == 2
    assert dispatcher.prompts[1][0].endswith(
        wrap_voice_prompt(
            "Subtract 38 from the multiplication result in this thread. Answer just the number"
        )
    )
    await bridge.aclose()


@pytest.mark.parametrize("final", ["desktop, the one from yesterday", "laptop instead"])
async def test_held_delegation_discards_pending_text_when_its_final_arrives(final):
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(settle=5.0)
    live.final("Read the file on my", item_id="first")
    bridge.on_user_state_changed("listening", "speaking")
    live.delegate("d1", "desktop")
    live.final(final, item_id="second")
    bridge.on_user_state_changed("speaking", "listening")
    bridge._flush_held()
    await _settle()
    assert len(dispatcher.prompts) == 1
    assert dispatcher.prompts[0][0].endswith(_voice(f"Read the file on my {final}"))
    assert not bridge._newest_entry_for(dispatcher).open_utterance
    await bridge.aclose()


async def test_reconnect_unbinds_a_delegation_while_its_utterance_is_held():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(settle=5.0)
    live.final("Run the tests")
    bridge.on_user_state_changed("listening", "speaking")
    live.delegate("old-session", "")
    assert bridge._held is not None
    assert bridge._held.delegation_id == "old-session"
    live.emit("session_reconnected")
    bridge.on_user_state_changed("speaking", "listening")
    bridge._flush_held()
    await _settle()
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("old-session") == []
    assert live.speech(None) == ["All tests pass. The build is green."]
    await bridge.aclose()


@pytest.mark.parametrize(
    "followup",
    [
        "Run the linter",
        "run the linter",
        "what time is it",
        "and also run the linter",
        "And then run the tests",
        "Thanks",
    ],
)
async def test_a_separate_quick_utterance_does_not_repeat_the_previous_command(
    followup,
):
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.final("Increment the counter")
    await _settle()
    live.final(followup)
    await _settle()
    if followup == "Thanks":
        assert len(dispatcher.prompts) == 1
    else:
        assert len(dispatcher.prompts) == 2
        assert dispatcher.prompts[1][0].endswith(_voice(followup))
        # A separate request never replaces the running turn: on a backend
        # that cannot steer, replacing would interrupt the counter increment.
        assert dispatcher.replaces == [False, False]
    await bridge.aclose()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (". Answer just the number", True),
        (", the one from yesterday", True),
        ("and answer just the number", True),
        ("And answer just the number", True),
        ("And also run the linter", False),
        ("and then run the tests", False),
        ("but only the first ten lines", True),
        ("or the staging one", True),
        ("answer just the number", False),
        ("run the linter", False),
        ("what time is it", False),
        ("Run the linter", False),
        ("Then run the tests", False),
        ("Also run the linter", False),
        ("What time is it", False),
        ("", False),
    ],
)
def test_looks_like_continuation(text, expected):
    from openbase_coder_cli.livekit_agent.live_delegation import looks_like_continuation

    assert looks_like_continuation(text) is expected


async def test_split_request_after_a_silent_delegation_replaces_the_fragment_turn():
    """The caller pauses mid-request while silent, the model delegates on the
    first half (which starts a turn at once), and the rest arrives a moment
    later: the merged request replaces that turn, inherits its delegation and
    speaks once. Without the replace flag a backend that cannot steer queued
    the whole request as a second turn (duplicate work, duplicate answer)."""
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(settle=0.05)
    bridge.on_user_state_changed("listening", "speaking")
    bridge.on_user_state_changed("speaking", "listening")
    live.final("Subtract 38 from the result in this thread")
    live.delegate("d1", "")
    await _settle()
    assert len(dispatcher.prompts) == 1
    assert dispatcher.replaces == [False]
    live.final("and answer just the number")
    await _settle()
    await asyncio.sleep(0.12)
    assert len(dispatcher.prompts) == 2
    assert dispatcher.prompts[1][0].endswith(
        _voice("Subtract 38 from the result in this thread and answer just the number")
    )
    assert dispatcher.replaces == [False, True]
    merged = bridge._newest_entry_for(dispatcher)
    assert merged.replaces_turn and merged.delegation_id == "d1"
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech() == ["All tests pass. The build is green."]
    assert live.speech("d1") == ["All tests pass. The build is green."]
    first = next(r for r in ledger._records.values() if r.message_id == "live-d1")
    assert first.status == "cancelled"
    await bridge.aclose()


async def test_split_request_merged_by_the_hold_does_not_replace_anything():
    """Fragments merged before any turn starts are one plain turn."""
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(settle=0.08)
    live.final("Subtract 38 from the result in this thread")
    await asyncio.sleep(0.03)
    live.final("and answer just the number")
    await asyncio.sleep(0.15)
    assert len(dispatcher.prompts) == 1
    assert dispatcher.replaces == [False]
    await bridge.aclose()


async def test_the_rest_of_a_delegated_open_utterance_replaces_its_turn():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(settle=0.05)
    live.final("What's in the grocery list file on my", item_id="speech_1")
    live.delegate("d1", "desktop")
    await _settle()
    await asyncio.sleep(0.12)
    live.final("desktop, the one from yesterday", item_id="speech_2")
    await _settle()
    assert len(dispatcher.prompts) == 2
    assert dispatcher.replaces == [False, True]
    await bridge.aclose()


async def test_repeated_open_delegations_preserve_the_held_request():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(settle=0.05)
    live.final("What's in the grocery list file on my", item_id="speech_1")
    live.delegate("d1", "desktop")
    await _settle()
    live.delegate("d2", "desktop, the one from yesterday")
    await _settle()
    live.final("desktop, the one from yesterday, read it aloud", item_id="speech_2")
    await _settle()
    assert dispatcher.replaces == [False, True, True]
    assert dispatcher.prompts[1][0].endswith(
        _voice("What's in the grocery list file on my desktop, the one from yesterday")
    )
    assert dispatcher.prompts[2][0].endswith(
        _voice(
            "What's in the grocery list file on my desktop, the one from yesterday, read it aloud"
        )
    )
    await bridge.aclose()


async def test_a_continuation_after_the_turn_spoke_is_a_new_request():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.final("Subtract 38 from the result in this thread")
    await _settle()
    dispatcher.progress("turn-1", _running_snapshot(lastUsefulMessage="It is 4."))
    live.final("and answer just the number")
    await _settle()
    assert len(dispatcher.prompts) == 2
    assert dispatcher.prompts[1][0].endswith(_voice("and answer just the number"))
    assert dispatcher.replaces == [False, False]
    await bridge.aclose()


def test_join_fragments_attaches_leading_punctuation():
    from openbase_coder_cli.livekit_agent.live_delegation import join_fragments

    assert join_fragments("in this thread", ". Answer just the number") == (
        "in this thread. Answer just the number"
    )
    assert join_fragments("What files are on my", "desktop") == (
        "What files are on my desktop"
    )
    assert join_fragments("", "desktop") == "desktop"


async def test_a_delegation_while_the_caller_is_silent_starts_the_turn_at_once():
    """A delegation from the model is the end-of-request signal: with the
    caller silent (session VAD) the agent hears the request without waiting
    for the settle window, so delegated requests pay no added latency."""
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        settle=5.0, hold_max=10.0, lag=5.0
    )
    bridge.on_user_state_changed("listening", "speaking")
    bridge.on_user_state_changed("speaking", "listening")
    live.final("What files are on my desktop")
    await _settle()
    assert dispatcher.prompts == []
    live.delegate("d1", "")
    await _settle()
    assert len(dispatcher.prompts) == 1
    assert dispatcher.prompts[0][0].endswith(_voice("What files are on my desktop"))
    dispatcher.result_gate.set()
    await _settle()
    assert live.speech("d1") == ["All tests pass. The build is green."]
    await bridge.aclose()


# --- log correlation ----------------------------------------------------------

BRIDGE_LOGGER = "openbase_coder_cli.livekit_agent.live_delegation"


def _lines(caplog, stage: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if f"stage={stage}" in record.getMessage()
    ]


async def test_every_bridge_line_names_the_call(caplog):
    """Service logs interleave calls; ``call=<room>`` picks one call's lines
    out and joins them with the gateway session and the turn store."""
    caplog.set_level(logging.INFO, logger=BRIDGE_LOGGER)
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        call_id="room-abc%1"
    )

    live.final("What is on my desktop?")
    await _settle()
    live.drop()
    dispatcher.result_gate.set()
    await _settle()
    live.emit("session_reconnected")
    await bridge.aclose()

    # Only the bridge's own lines: the delivery ledger logs dispatch_timing
    # lines too, and they reach caplog once any earlier test configured
    # Django logging (root at INFO).
    messages = [
        r.getMessage()
        for r in caplog.records
        if r.name == BRIDGE_LOGGER and "dispatch_timing" in r.getMessage()
    ]
    assert messages, "no bridge log lines"
    assert all(m.endswith(" call=room-abc%1") for m in messages), messages
    assert _lines(caplog, "live_forced_delegation")
    assert _lines(caplog, "live_append_instructions")
    for stage in (
        "live_session_dropped",
        "live_commentary_held",
        "live_session_reconnected",
        "live_commentary_redelivered",
    ):
        assert _lines(caplog, stage)
    assert "drop_seen=True" in _lines(caplog, "live_session_reconnected")[0]


async def test_bridge_lines_carry_no_call_tag_without_a_call_id(caplog):
    caplog.set_level(logging.INFO, logger=BRIDGE_LOGGER)
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()

    live.final("What is on my desktop?")
    await _settle()
    await bridge.aclose()

    messages = [
        r.getMessage() for r in caplog.records if "dispatch_timing" in r.getMessage()
    ]
    assert messages and not any(" call=" in m for m in messages)


async def test_turn_binding_is_logged_once_per_utterance(caplog):
    """The line that joins a spoken request (``key``) to the Super Agents turn
    row (``turn_id``): emitted when the turn id first becomes known, from a
    progress snapshot or from the result, never twice for the same turn."""
    caplog.set_level(logging.INFO, logger=BRIDGE_LOGGER)
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(call_id="room-1")

    live.delegate("d1", "Check whether the build passes")
    await _settle()
    dispatcher.progress("turn-1", {"status": "running"})
    dispatcher.result_gate.set()
    await _settle()

    bound = _lines(caplog, "live_delegation_turn_bound")
    assert len(bound) == 1, bound
    assert "key=d1" in bound[0]
    assert "turn_id=turn-1" in bound[0]
    assert "delegation_id=d1" in bound[0]
    assert "source=progress" in bound[0]
    assert "active_thread_id=dispatcher-thread" in bound[0]
    await bridge.aclose()


async def test_turn_binding_from_the_result_when_no_progress_arrived(caplog):
    caplog.set_level(logging.INFO, logger=BRIDGE_LOGGER)
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()

    live.final("Run the tests")
    await _settle()
    dispatcher.result_gate.set()
    await _settle()

    bound = _lines(caplog, "live_delegation_turn_bound")
    assert len(bound) == 1, bound
    assert "key=utt-1" in bound[0] and "turn_id=turn-1" in bound[0]
    assert "source=result" in bound[0]
    await bridge.aclose()


async def test_closing_the_bridge_logs_one_call_summary(caplog):
    caplog.set_level(logging.INFO, logger=BRIDGE_LOGGER)
    now = [100.0]
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        call_id="room-1", clock=lambda: now[0]
    )

    live.delegate("d1", "Check whether the build passes")
    await _settle()
    dispatcher.result_gate.set()
    await _settle()
    now[0] = 142.25
    await bridge.aclose()
    await bridge.aclose()

    summary = _lines(caplog, "live_call_summary")
    assert len(summary) == 1, summary
    line = summary[0]
    assert "duration_s=42.2" in line, line
    assert "utterances=1" in line
    assert "decision_started=1" in line
    assert "delegations_created=1" in line
    assert "turns_bound=1" in line
    assert "append_instructions=1" in line
    assert "append_thinking=" in line
    assert "superseded=0" in line
    assert line.endswith(" call=room-1")


async def test_call_summary_keeps_totals_after_entries_are_pruned(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger=BRIDGE_LOGGER)
    monkeypatch.setattr(live_delegation, "MAX_TRACKED_ENTRIES", 1)
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    dispatcher.result_gate.set()

    for index in range(3):
        live.final(f"Run the tests for package {index}")
        await _settle()
    await bridge.aclose()

    assert len(bridge._entries) == 1
    summary = _lines(caplog, "live_call_summary")[0]
    assert "utterances=3" in summary
    assert "superseded=2" in summary


@pytest.mark.parametrize("args", [(), ("value",)])
def test_call_tag_preserves_percent_with_and_without_format_args(caplog, args):
    caplog.set_level(logging.DEBUG, logger=BRIDGE_LOGGER)
    adapter = live_delegation._CallLogAdapter(
        logging.getLogger(BRIDGE_LOGGER), "room-abc%1"
    )

    adapter.debug("diagnostic %s" if args else "diagnostic", *args)

    assert caplog.records[-1].getMessage().endswith(" call=room-abc%1")


async def test_complete_backend_answer_uses_one_explicit_speech_command():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    answer = "The tests passed. The build still needs a signing certificate."
    _answer(dispatcher, answer)
    live.delegate("d1", "Give me the test result and the build blocker")
    await _settle()
    dispatcher.progress("turn-1", _running_snapshot(lastUsefulMessage=answer))
    dispatcher.result_gate.set()
    await _settle()
    commands = [
        text for text in live.of("instructions", "d1") if "Text to read: " in text
    ]
    assert len(commands) == 1
    assert "in full" in commands[0]
    assert json.loads(commands[0].split("Text to read: ", 1)[1]) == answer
    assert live.of("commentary", "d1") == []
    assert ledger.record_for_turn("turn-1").status == "text_generated"
    await bridge.aclose()


# --- speakerphone echo and starved answers -----------------------------------


async def test_a_lone_short_word_over_the_agents_speech_is_dropped_as_echo():
    """ "...Cooper" heard back as "uper" must not interrupt with a turn that
    answers nothing (2026-10-10, Android speakerphone)."""
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    try:
        bridge.on_agent_state_changed("listening", "speaking")
        bridge.on_user_state_changed("listening", "speaking")
        bridge.on_user_state_changed("speaking", "listening")
        live.final("uper")
        await _settle()
        assert dispatcher.prompts == []
        assert bridge._stats["decision_ignored_echo_fragment"] == 1

        # The same fragment while the agent is quiet is the caller's own word.
        bridge.on_agent_state_changed("speaking", "listening")
        bridge.on_user_state_changed("listening", "speaking")
        bridge.on_user_state_changed("speaking", "listening")
        live.final("Run.")
        await _settle()
        assert len(dispatcher.prompts) == 1
    finally:
        await bridge.aclose()


async def test_a_real_question_over_the_agents_speech_still_reaches_the_agent():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    try:
        bridge.on_agent_state_changed("listening", "speaking")
        bridge.on_user_state_changed("listening", "speaking")
        bridge.on_user_state_changed("speaking", "listening")
        live.final("Stop. What is seven times eight?")
        await _settle()
        assert len(dispatcher.prompts) == 1
    finally:
        await bridge.aclose()


async def test_an_answer_the_gate_starved_is_asked_for_again_once(monkeypatch):
    """The owed answer was spoken into a discarded burst; after that burst
    closes with nothing permitted played, the bridge re-asks for it once."""
    import openbase_coder_cli.livekit_agent.live_delegation as module

    monkeypatch.setattr(module, "STARVED_REDELIVERY_GRACE_SECONDS", 0.0)
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    try:
        live.delegate("d1", "What did Cooper say about occupied squares")
        await _settle()
        dispatcher.result_gate.set()
        await _settle()
        assert live.speech() == ["All tests pass. The build is green."]
        assert bridge.speech_gate.authorized

        bridge.speech_gate.on_starved()
        await _settle()
        assert live.speech() == ["All tests pass. The build is green."] * 2
        notes = live.of("thinking", None)
        assert notes and "could not hear" in notes[-1]

        # Re-asked once: a second starved burst does not produce a third copy.
        bridge.speech_gate.on_starved()
        await _settle()
        assert live.speech() == ["All tests pass. The build is green."] * 2
    finally:
        await bridge.aclose()


async def test_a_starved_report_after_the_model_spoke_is_not_redelivered(monkeypatch):
    import openbase_coder_cli.livekit_agent.live_delegation as module

    monkeypatch.setattr(module, "STARVED_REDELIVERY_GRACE_SECONDS", 0.0)
    clock = {"now": 100.0}
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge(
        clock=lambda: clock["now"]
    )
    try:
        live.delegate("d1", "Check the build")
        await _settle()
        dispatcher.result_gate.set()
        await _settle()
        clock["now"] += 1.0
        bridge.on_agent_state_changed("listening", "speaking")  # it was heard
        bridge.speech_gate.on_starved()
        await _settle()
        assert live.speech() == ["All tests pass. The build is green."]
    finally:
        await bridge.aclose()


async def test_gateway_response_lifecycle_is_logged_but_deltas_stay_quiet(caplog):
    import logging

    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    try:
        with caplog.at_level(
            logging.INFO, logger="openbase_coder_cli.livekit_agent.live_delegation"
        ):
            live.emit(
                "openai_server_event_received",
                {"type": "session.output_audio.delta", "delta": "AAAA"},
            )
            live.emit(
                "openai_server_event_received",
                {
                    "type": "response.event",
                    "delegation_id": "d1",
                    "event": {"type": "response.output_text.delta", "delta": "x"},
                },
            )
            live.emit(
                "openai_server_event_received",
                {
                    "type": "response.event",
                    "delegation_id": "d1",
                    "event": {
                        "type": "response.completed",
                        "response": {
                            "status": "incomplete",
                            "incomplete_details": {"reason": "max_output_tokens"},
                        },
                    },
                },
            )
            live.emit(
                "openai_server_event_received",
                {
                    "type": "error",
                    "error": {"code": "rate_limited", "type": "server_error"},
                },
            )
            live.emit(
                "openai_server_event_received",
                {"type": "session.closed", "reason": "allowance_exhausted"},
            )
        lines = [
            r.getMessage()
            for r in caplog.records
            if "live_gateway_event" in r.getMessage()
        ]
        assert len(lines) == 3
        assert (
            "type=response.completed delegation_id=d1 status=incomplete incomplete=reason:max_output_tokens"
            in lines[0]
        )
        assert (
            "type=error delegation_id= code=rate_limited error_type=server_error"
            in lines[1]
        )
        assert (
            "type=session.closed delegation_id= reason=allowance_exhausted" in lines[2]
        )
    finally:
        await bridge.aclose()


async def test_output_transcript_deltas_are_logged_per_burst(caplog):
    import logging

    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    try:
        with caplog.at_level(
            logging.INFO, logger="openbase_coder_cli.livekit_agent.live_delegation"
        ):
            for piece in ("I'm not ", "in a dispatcher ", "session."):
                live.emit(
                    "openai_server_event_received",
                    {"type": "session.output_transcript.delta", "delta": piece},
                )
            live.emit(
                "openai_server_event_received",
                {"type": "session.closed", "reason": "done"},
            )
        lines = [
            r.getMessage()
            for r in caplog.records
            if "live_output_transcript" in r.getMessage()
        ]
        assert len(lines) == 1
        assert "chars=32" in lines[0] and "I'm not in a dispatcher session." in lines[0]
    finally:
        await bridge.aclose()
