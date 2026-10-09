"""Tier-1 tests for the GPT-Live client delegation bridge (no network)."""

from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace

import pytest

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

    async def run_turn(self, prompt, *, developer_instructions=None):
        self.prompts.append((prompt, developer_instructions))
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
        **({"clock": clock} if clock is not None else {}),
    )
    bridge.attach(live)
    return bridge, live, router, dispatcher, delivery_ledger, lifecycle


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
    assert live.of("commentary") == []
    assert ("utterance_accepted", "live-d1") in lifecycle

    dispatcher.result_gate.set()
    await _settle()

    commentary = live.of("commentary", "d1")
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
    assert live.of("commentary", "d1") == ["Tests are running. Twelve passed so far."]

    dispatcher.result = {
        "_livekit_speech_text": "Tests are running. Twelve passed so far. All done.",
        "_livekit_turn_id": "turn-1",
        "status": "completed",
        "progress": {},
    }
    dispatcher.result_gate.set()
    await _settle()
    assert live.of("commentary", "d1") == [
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

    assert live.of("commentary") == []
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

    assert live.of("commentary", "d1") == ["Checking your desktop now."]
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

    assert live.of("commentary") == ["Your desktop is empty."]
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

    assert live.of("commentary", "d1") == [answer]
    assert not any("already answered" in note for note in live.of("thinking", "d1"))
    record = ledger.record_for_turn("turn-1")
    assert record is not None
    assert record.status == "cancelled"
    assert record.terminal_reason == "live_answer_already_spoken"
    assert not ledger.has_pending_delivery_for_current_route()
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
    assert live.of("commentary", "d1") == ["All tests pass. The build is green."]
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
    assert live.of("commentary", "d1") == ["All tests pass. The build is green."]
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

    assert live.of("commentary", "d1") == []
    assert live.of("commentary", None) == ["All tests pass. The build is green."]
    (briefing,) = [t for t in live.of("thinking", None) if "re-established" in t]
    assert "Do not greet the caller again" in briefing
    assert "the dispatcher is still working" in briefing
    await bridge.aclose()


async def test_a_reconnect_with_nothing_running_briefs_without_a_pending_note():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.emit("session_reconnected")
    await _settle()
    (briefing,) = live.of("thinking", None)
    assert "re-established" in briefing
    assert "still working" not in briefing
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


async def test_pending_approval_is_spoken_once_and_retained_as_instructions():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "Deploy it")
    await _settle()
    snapshot = {"status": "waiting", "pendingRequests": [{"id": "approval-1"}]}
    dispatcher.progress("turn-1", snapshot)
    dispatcher.progress("turn-1", snapshot)
    assert live.of("commentary", "d1") == [LIVE_APPROVAL_PENDING_COMMENTARY]
    assert len(live.of("instructions", "d1")) == 1
    assert "Approvals" in live.of("instructions", "d1")[0]
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
    assert live.of("commentary", "d1") == [LIVE_EMPTY_ANSWER_COMMENTARY]
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
    assert live.of("commentary") == []
    assert ("utterance_accepted", "live-utt-1") in lifecycle

    dispatcher.result_gate.set()
    await _settle()
    assert live.of("commentary", None) == [
        "Your desktop has two folders and a screenshot."
    ]
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
    assert live.of("commentary", "d1") == ["All tests pass. The build is green."]
    assert live.of("commentary", None) == []
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
    assert live.of("commentary", "d1") == ["All tests pass. The build is green."]
    assert live.of("commentary", None) == []
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
    assert live.of("commentary", "d1") == ["All tests pass. The build is green."]
    assert live.of("commentary", None) == []
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
    # Both reach the thread; run_turn steers the running turn with the second.
    assert len(dispatcher.prompts) == 2
    assert dispatcher.prompts[0][0].endswith(wrap_voice_prompt("Check the build"))
    assert dispatcher.prompts[1][0].endswith(
        wrap_voice_prompt("And also run the linter")
    )
    # A late delegation binds to the newest utterance.
    live.delegate("d1", "")
    await _settle()

    dispatcher.result_gate.set()
    await _settle()
    # The superseded first result is dropped; the merged answer speaks once.
    assert live.of("commentary") == ["All tests pass. The build is green."]
    assert live.of("commentary", "d1") == ["All tests pass. The build is green."]
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
    assert live.of("commentary", "d1") == ["All tests pass. The build is green."]
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
    assert live.of("commentary", "d1") == ["All tests pass. The build is green."]
    await bridge.aclose()


async def test_late_delegation_for_an_answered_utterance_is_not_rerun():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.final("Check the build")
    dispatcher.result_gate.set()
    await _settle()
    assert live.of("commentary", None) == ["All tests pass. The build is green."]

    live.delegate("d1", "")
    await _settle()
    assert len(dispatcher.prompts) == 1
    (thinking,) = live.of("thinking", "d1")
    assert "already answered" in thinking
    assert live.of("commentary", "d1") == []
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
    assert live.of("commentary", "d1") == [BACK_TO_DISPATCH_COMMENTARY]
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
    assert live.of("commentary", "d1") == ["All tests pass. The build is green."]
    await bridge.aclose()


async def test_bridge_subscribes_to_closed_utterances_and_delegations():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    assert len(live._handlers["input_audio_transcription_completed"]) == 1
    assert len(live._handlers["delegation_created"]) == 1
    await bridge.aclose()
    assert live._handlers["input_audio_transcription_completed"] == []
    assert live._handlers["delegation_created"] == []


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

    assert live.of("commentary", "d1") == []
    assert live.of("commentary", "d2") == ["All tests pass. The build is green."]
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
    assert live.of("commentary", "d1") == []
    await bridge.aclose()


# --- route changes and spoken commands -----------------------------------------


async def test_route_change_appends_thinking_and_a_spoken_handoff_naming_the_agent():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    bridge.notify_route_changed(action="transfer_to_thread", agent_label="Lucy")
    thinking = live.of("thinking", None)
    assert thinking and "Lucy" in thinking[0]
    assert "Direct route guidance." in thinking[0]
    assert live.of("commentary", None) == ["You are now talking to Lucy."]
    assert bridge.active_agent_label == "Lucy"

    bridge.notify_route_changed(action="exit_to_dispatch", agent_label=None)
    assert live.of("commentary", None)[-1] == BACK_TO_DISPATCH_COMMENTARY
    assert bridge.active_agent_label == live_delegation.DISPATCHER_AGENT_LABEL
    await bridge.aclose()


async def test_exit_to_dispatch_spoken_on_the_transcript_switches_route_without_a_turn():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    other = FakeVoiceClient(thread_id="thread-2")
    router.transfer(other)

    bridge.on_user_transcript("Exit to dispatch.", is_final=True)
    assert router.is_dispatcher_active
    assert live.of("commentary", None) == [BACK_TO_DISPATCH_COMMENTARY]

    # The model delegates the same utterance a moment later: it is answered
    # as a command receipt, never sent to the dispatcher as a prompt.
    live.delegate("d1", "Exit to dispatch.")
    await _settle()
    assert dispatcher.prompts == []
    assert other.prompts == []
    assert live.of("commentary", "d1") == [BACK_TO_DISPATCH_COMMENTARY]
    await bridge.aclose()


async def test_exit_to_dispatch_delegated_first_switches_route():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    other = FakeVoiceClient(thread_id="thread-2")
    router.transfer(other)
    live.delegate("d1", "to dispatch")
    await _settle()
    assert router.is_dispatcher_active
    assert live.of("commentary", "d1") == [BACK_TO_DISPATCH_COMMENTARY]
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
    assert live.of("commentary", "d1") == [expected]
    record = next(r for r in ledger._records.values() if r.message_id == "live-d1")
    assert record.status == "cancelled"
    await bridge.aclose()


# --- announcer ------------------------------------------------------------------


async def test_user_say_announcements_become_session_wide_commentary():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    bridge.announce("Finished the report.", agent_name="Lucy")
    bridge.announce("Back to dispatch.")
    assert live.of("commentary", None) == [
        "Lucy: Finished the report.",
        "Back to dispatch.",
    ]
    await bridge.aclose()


async def test_orphaned_results_are_spoken_once():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    bridge.deliver_orphaned_result(dispatcher, "turn-9", "The deploy finished.")
    bridge.deliver_orphaned_result(dispatcher, "turn-9", "The deploy finished.")
    assert live.of("commentary", None) == ["The deploy finished."]
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
    assert live.of("commentary", "d1") == ["All tests pass. The build is green."]
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
    assert live.of("commentary", "old-session") == []
    assert live.of("commentary", None) == ["All tests pass. The build is green."]
    await bridge.aclose()


@pytest.mark.parametrize("followup", ["Run the linter", "Thanks"])
async def test_a_separate_quick_utterance_does_not_repeat_the_previous_command(followup):
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
    assert live.of("commentary", "d1") == ["All tests pass. The build is green."]
    await bridge.aclose()
