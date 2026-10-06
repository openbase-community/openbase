"""Tier-1 tests for the GPT-Live client delegation bridge (no network)."""

from __future__ import annotations

import asyncio
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
    LiveTranscriptBuffer,
    chunk_commentary,
    estimate_tokens,
)
from openbase_coder_cli.livekit_agent.voice_delivery import (
    VoiceDeliveryLedger,
    VoiceRouteSnapshot,
)
from openbase_coder_cli.voice_tags import VOICE_TAG_CLOSE, VOICE_TAG_OPEN


class FakeGPTLiveSession:
    """A ``GPTLiveSession``-like emitter: the three appends plus events."""

    def __init__(self) -> None:
        self.appends: list[tuple[str, str, str | None]] = []
        self._handlers: dict[str, list] = {}

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


def _make_bridge(*, ledger=True, developer_instructions="Direct route guidance."):
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
    )
    bridge.attach(live)
    return bridge, live, router, dispatcher, delivery_ledger, lifecycle


async def _settle():
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


# --- transcript buffer --------------------------------------------------------


def test_transcript_buffer_combines_unseen_finals_with_the_pending_utterance():
    clock = {"now": 100.0}
    buffer = LiveTranscriptBuffer(clock=lambda: clock["now"])
    buffer.note_final("Hey there.")
    buffer.note_final("Can you check the build")
    assert buffer.take_prompt("and run the tests") == (
        "Hey there. Can you check the build and run the tests"
    )
    # The final for the consumed open utterance is not replayed, but an
    # extension beyond it is.
    buffer.note_final("and run the tests, please, twice")
    assert buffer.take_prompt("") == "please, twice"
    buffer.note_final("stale small talk")
    clock["now"] += 500
    assert buffer.take_prompt("new request") == "new request"


def test_transcript_buffer_does_not_duplicate_a_final_that_is_a_prefix_of_pending():
    buffer = LiveTranscriptBuffer()
    buffer.note_final("Check the build")
    assert (
        buffer.take_prompt("Check the build and deploy") == "Check the build and deploy"
    )


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


async def test_delegation_without_new_speech_is_answered_from_context_only():
    bridge, live, router, dispatcher, ledger, lifecycle = _make_bridge()
    live.delegate("d1", "")
    await _settle()
    assert dispatcher.prompts == []
    assert (
        live.of("thinking", "d1")
        and "answer from the conversation" in (live.of("thinking", "d1")[0])
    )
    assert lifecycle == []
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
    assert dispatcher.claimed == []
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
