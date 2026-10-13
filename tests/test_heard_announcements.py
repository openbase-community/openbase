"""Background announcements reach the call target as text, once per target."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import llm
from test_live_delegation import FakeVoiceClient, _make_bridge, _settle
from test_livekit_agent_announcer import FakeSession, FakeTTS

from openbase_coder_cli.livekit_agent.codex_llm import CodexLLMStream
from openbase_coder_cli.livekit_agent.heard_announcements import (
    MAX_AGE_SECONDS,
    MAX_HEARD,
    MAX_TEXT_CHARS,
    UNNAMED_ANNOUNCER,
    HeardAnnouncements,
    apply_heard_announcements,
)
from openbase_coder_cli.livekit_agent.live_characters import LiveCharacterController
from openbase_coder_cli.livekit_agent.packets import AnnouncerMessage
from openbase_coder_cli.livekit_agent.speech_queue import AnnouncerSpeechQueue
from openbase_coder_cli.livekit_agent.voice_routing import LiveKitVoiceRouter
from openbase_coder_cli.voice_tags import VOICE_TAG_CLOSE, VOICE_TAG_OPEN

NOTE_START = "[Openbase system note: background announcements"


class Target:
    """A backend client stand-in (weak-referenceable, unlike ``object()``)."""


def note_data(prompt: str) -> dict:
    """The quoted announcements in front of ``prompt``."""
    assert prompt.startswith(NOTE_START), prompt[:120]
    note, _rest = prompt.split("]\n\n", 1)
    return json.loads(note.split("Data: ", 1)[1])


def has_note(prompt: str) -> bool:
    return prompt.startswith(NOTE_START)


class Clock:
    def __init__(self) -> None:
        self.monotonic = 1000.0
        self.wall = datetime(2026, 10, 13, 14, 2, 11, tzinfo=timezone.utc)

    def ledger(self) -> HeardAnnouncements:
        return HeardAnnouncements(
            clock=lambda: self.monotonic, wall_clock=lambda: self.wall
        )


# --- the ledger ---------------------------------------------------------------


def test_note_carries_announcer_text_time_and_playback_and_is_told_once():
    heard = Clock().ledger()
    target = Target()
    heard.record(
        message_id="announcer-1",
        text="Hey Gabe, the  iOS build\nis green.",
        agent_name="Marian",
    )
    heard.record(
        message_id="announcer-2",
        text="Hi, I'm Oliver.",
        agent_name="Oliver",
        completed=False,
    )

    prompt = heard.apply(target, "What was that about?")

    assert prompt.endswith("]\n\nWhat was that about?")
    data = note_data(prompt)
    assert data["0"] == {
        "announced_by": "Marian",
        "text": "Hey Gabe, the iOS build is green.",
        "spoken_at": "2026-10-13T14:02:11Z",
        "playback": "completed",
    }
    assert data["1"]["announced_by"] == "Oliver"
    assert data["1"]["playback"] == "cut off"
    assert "not a message from the caller" in prompt
    assert "Do not reply to them" in prompt
    # Told once: the next turn to the same target carries nothing.
    assert heard.apply(target, "Thanks.") == "Thanks."


def test_brackets_inside_quoted_data_do_not_break_the_single_envelope():
    heard = Clock().ledger()
    heard.record(message_id="a", text="Done [with] the task", agent_name="A[x]")
    prompt = heard.apply(Target(), "hi")
    note = prompt.split("\n\n", 1)[0]
    assert note.count("[") == 1 and note.count("]") == 1
    assert note_data(prompt)["0"] == {
        "announced_by": "A[x]",
        "text": "Done [with] the task",
        "spoken_at": "2026-10-13T14:02:11Z",
        "playback": "completed",
    }


def test_internal_cues_empty_text_and_repeats_are_not_announcements():
    heard = Clock().ledger()
    assert heard.record(message_id="voice-route-1", text="Back to dispatch.") is None
    assert (
        heard.record(
            message_id="transcription-recovery-x", text="I did not catch that."
        )
        is None
    )
    assert heard.record(message_id="announcer-3", text="   ") is None
    first = heard.record(message_id="announcer-4", text="Hey Gabe, done.")
    assert first is not None
    assert heard.record(message_id="announcer-4", text="Hey Gabe, done.") is None
    assert heard.apply(Target(), "x") == heard.apply(Target(), "x")
    assert len(note_data(heard.apply(Target(), "x"))) == 1


def test_unnamed_announcer_and_long_text_are_bounded():
    heard = Clock().ledger()
    heard.record(message_id="announcer-5", text="y" * 5000)
    data = note_data(heard.apply(Target(), "x"))["0"]
    assert data["announced_by"] == UNNAMED_ANNOUNCER
    assert len(data["text"]) == MAX_TEXT_CHARS + 1 and data["text"].endswith("…")


def test_only_recent_announcements_are_kept_and_relayed():
    clock = Clock()
    heard = clock.ledger()
    for i in range(MAX_HEARD + 3):
        heard.record(message_id=f"announcer-{i}", text=f"update {i}")
    data = note_data(heard.apply(Target(), "x"))
    assert len(data) == MAX_HEARD
    assert data["0"]["text"] == "update 3"

    heard = clock.ledger()
    heard.record(message_id="old", text="old news")
    clock.monotonic += MAX_AGE_SECONDS + 1
    heard.record(message_id="new", text="fresh news")
    data = note_data(heard.apply(Target(), "x"))
    assert [item["text"] for item in data.values()] == ["fresh news"]


def test_each_call_target_is_told_separately_across_transfers():
    heard = Clock().ledger()
    dispatcher, marian = Target(), Target()
    heard.record(message_id="a1", text="Hey Gabe, tests pass.", agent_name="Asher")
    assert has_note(heard.apply(dispatcher, "ok"))
    # Transferred to Marian: she has not been told, so she is.
    assert note_data(heard.apply(marian, "hi"))["0"]["announced_by"] == "Asher"
    heard.record(message_id="a2", text="Hey Gabe, deploy finished.", agent_name="Sarah")
    assert [i["text"] for i in note_data(heard.apply(marian, "hi")).values()] == [
        "Hey Gabe, deploy finished."
    ]
    # Back to the Dispatcher: only what it missed while the call was away.
    assert [i["text"] for i in note_data(heard.apply(dispatcher, "x")).values()] == [
        "Hey Gabe, deploy finished."
    ]
    heard.clear()
    assert heard.apply(marian, "x") == "x"


def test_routers_without_the_ledger_pass_prompts_through():
    router = SimpleNamespace(active_client=Target())
    assert apply_heard_announcements(router, "p") == "p"
    assert apply_heard_announcements(SimpleNamespace(), "p") == "p"


# --- Classic: announcer queue playout -> ledger -> next backend turn ----------


async def _classic_turn(router, text: str) -> None:
    """One caller utterance through the pipeline engine's LLM bridge."""
    stream = SimpleNamespace(
        _voice_router=router,
        _buffered_input=None,
        _message_id="m-1",
        _event_ch=SimpleNamespace(closed=False, send_nowait=lambda chunk: None),
        _emit_delta=lambda text: None,
    )
    await CodexLLMStream._run_accepted_prompt(stream, text, None, None)


async def test_classic_announcement_reaches_the_dispatcher_on_its_next_turn():
    """Harness: a ``user say`` announcement during a simulated Classic call."""
    dispatcher = FakeVoiceClient(thread_id="dispatcher-thread")
    dispatcher.result_gate.set()
    router = LiveKitVoiceRouter(dispatcher)
    session = FakeSession()
    queue = AnnouncerSpeechQueue(
        session=session,
        announcer_tts=FakeTTS(),
        silence_grace_seconds=0,
        heard_sink=router.heard_announcements.record,
    )

    # The caller speaks before anything was announced: no note.
    await _classic_turn(router, "Start the build.")
    assert not has_note(dispatcher.prompts[0][0])

    # ``openbase-coder user say Marian "Hey Gabe, the build is green."``
    await queue._speak(
        AnnouncerMessage(
            message_id="announcer-1",
            text="Hey Gabe, the build is green.",
            voice_id="marian-voice",
            agent_name="Marian",
        )
    )
    assert session.say_calls, "the announcement did not play"

    await _classic_turn(router, "What was that about?")
    prompt = dispatcher.prompts[1][0]
    print("\n--- Classic: Dispatcher turn after the announcement ---\n" + prompt)
    data = note_data(prompt)
    assert data["0"]["announced_by"] == "Marian"
    assert data["0"]["text"] == "Hey Gabe, the build is green."
    assert data["0"]["playback"] == "completed"
    assert prompt.endswith(f"What was that about?{VOICE_TAG_CLOSE}")
    assert VOICE_TAG_OPEN in prompt

    await _classic_turn(router, "Thanks.")
    assert not has_note(dispatcher.prompts[2][0])
    await queue.close()


async def test_classic_interrupted_announcement_is_relayed_as_cut_off():
    heard = Clock().ledger()
    session = FakeSession()
    session.say_handle.interrupted = True
    queue = AnnouncerSpeechQueue(
        session=session,
        announcer_tts=FakeTTS(),
        silence_grace_seconds=0,
        heard_sink=heard.record,
    )
    await queue._speak(
        AnnouncerMessage(
            message_id="announcer-2", text="Hi, I'm Oliver.", agent_name="Oliver"
        )
    )
    assert note_data(heard.apply(Target(), "x"))["0"]["playback"] == "cut off"
    await queue.close()


async def test_classic_route_cues_are_not_relayed():
    heard = Clock().ledger()
    queue = AnnouncerSpeechQueue(
        session=FakeSession(),
        announcer_tts=FakeTTS(),
        silence_grace_seconds=0,
        heard_sink=heard.record,
    )
    await queue._speak(
        AnnouncerMessage(
            message_id="voice-route-1", text="You're now talking with Marian."
        )
    )
    assert heard.apply(Target(), "x") == "x"
    await queue.close()


async def test_a_failing_sink_never_breaks_playout():
    def broken(**_kwargs):
        raise RuntimeError("boom")

    session = FakeSession()
    queue = AnnouncerSpeechQueue(
        session=session,
        announcer_tts=FakeTTS(),
        silence_grace_seconds=0,
        heard_sink=broken,
    )
    await queue._speak(AnnouncerMessage(message_id="announcer-3", text="Hey Gabe."))
    assert session.say_handle.waited is True
    await queue.close()


# --- GPT-Live: character playout -> ledger -> delegated turn, across a transfer


async def _play_live_announcement(router, message: AnnouncerMessage, monkeypatch):
    """Drive the character controller through one announcement playout."""
    import openbase_coder_cli.livekit_agent.live_characters as module

    session = SimpleNamespace(
        current_agent=SimpleNamespace(chat_ctx=llm.ChatContext()),
        agent_state="listening",
        user_state="listening",
        output=Mock(audio_enabled=True),
        interrupt=Mock(side_effect=lambda **_: AsyncMock()()),
    )
    controller = LiveCharacterController(
        session=session,
        bridge=Mock(),
        router=router,
        model_factory=Mock(),
        instructions=Mock(),
        on_error=AsyncMock(),
    )
    monkeypatch.setattr(
        module,
        "agent_voice_identity",
        lambda _: SimpleNamespace(gpt_live_voice="cedar", voice_name="Marian"),
    )
    live = Mock()

    async def replace(**kwargs):
        kwargs["on_enter"](live)
        return SimpleNamespace(duplex_session=live)

    controller._replace = replace
    controller._conversation = AsyncMock()
    # The character speaks each chunk to the end: a completed playout.
    controller._wait_announcement_speech = AsyncMock()
    await controller._announcement(message)
    assert live.append_instructions.called, "the announcement did not play"


async def test_live_announcement_reaches_the_current_target_and_follows_a_transfer(
    monkeypatch,
):
    """Harness: an announcement during a simulated GPT-Live call, then a transfer."""
    bridge, live, router, dispatcher, _ledger, _lifecycle = _make_bridge()
    router.heard_announcements = HeardAnnouncements()
    dispatcher.result_gate.set()
    marian = FakeVoiceClient(thread_id="marian-thread")
    marian.result_gate.set()

    live.delegate("d0", "Start the build.")
    await _settle()
    assert not has_note(dispatcher.prompts[0][0])

    await _play_live_announcement(
        router,
        AnnouncerMessage(
            message_id="announcer-asher-done",
            text="Hey Gabe, Asher here: the migration finished.",
            voice_id="asher-voice",
            agent_name="Asher",
        ),
        monkeypatch,
    )

    live.delegate("d1", "What did that mean?")
    await _settle()
    prompt = dispatcher.prompts[1][0]
    print("\n--- GPT-Live: Dispatcher turn after the announcement ---\n" + prompt)
    data = note_data(prompt)
    assert data["0"]["announced_by"] == "Asher"
    assert data["0"]["text"] == "Hey Gabe, Asher here: the migration finished."
    assert data["0"]["playback"] == "completed"
    assert prompt.endswith(f"What did that mean?{VOICE_TAG_CLOSE}")

    # Transferred to Marian: she is told what the caller heard, once.
    router.transfer(marian)
    bridge.notify_route_changed(action="transfer_to_thread", agent_label="Marian")
    live.delegate("d2", "Marian, was that about your migration?")
    await _settle()
    prompt = marian.prompts[0][0]
    print("\n--- GPT-Live: Marian's first turn after the transfer ---\n" + prompt)
    assert note_data(prompt)["0"]["announced_by"] == "Asher"

    await _play_live_announcement(
        router,
        AnnouncerMessage(
            message_id="announcer-sarah-1",
            text="Hey Gabe, Sarah here: deploy is live.",
            voice_id="sarah-voice",
            agent_name="Sarah",
        ),
        monkeypatch,
    )
    live.delegate("d3", "Marian, go on.")
    await _settle()
    data = note_data(marian.prompts[1][0])
    assert [item["announced_by"] for item in data.values()] == ["Sarah"]

    # Back to the Dispatcher: only Sarah's, which it missed while away.
    router.exit_to_dispatch()
    bridge.notify_route_changed(action="exit_to_dispatch", agent_label=None)
    live.delegate("d4", "Back with you.")
    await _settle()
    data = note_data(dispatcher.prompts[2][0])
    assert [item["announced_by"] for item in data.values()] == ["Sarah"]

    live.delegate("d5", "Thanks.")
    await _settle()
    assert not has_note(dispatcher.prompts[3][0])
    await bridge.aclose()


async def test_live_cut_off_announcement_is_relayed_as_cut_off(monkeypatch):
    bridge, live, router, dispatcher, _ledger, _lifecycle = _make_bridge()
    router.heard_announcements = HeardAnnouncements()
    import openbase_coder_cli.livekit_agent.live_characters as module

    controller = LiveCharacterController(
        session=SimpleNamespace(
            current_agent=SimpleNamespace(chat_ctx=llm.ChatContext()),
            agent_state="listening",
            user_state="listening",
            output=Mock(audio_enabled=True),
            interrupt=Mock(side_effect=lambda **_: AsyncMock()()),
        ),
        bridge=Mock(),
        router=router,
        model_factory=Mock(),
        instructions=Mock(),
        on_error=AsyncMock(),
        timeout=0.01,
    )
    monkeypatch.setattr(
        module,
        "agent_voice_identity",
        lambda _: SimpleNamespace(gpt_live_voice="cedar", voice_name="Oliver"),
    )
    live_model = Mock()

    async def replace(**kwargs):
        kwargs["on_enter"](live_model)
        return SimpleNamespace(duplex_session=live_model)

    controller._replace = replace
    controller._conversation = AsyncMock()
    # Nothing ever reports speech, so the announcement times out unspoken.
    await controller._announcement(
        AnnouncerMessage(
            message_id="announcer-9", text="Hi, I'm Oliver.", agent_name="Oliver"
        )
    )
    pending = router.heard_announcements.pending_for(dispatcher)
    assert [item.completed for item in pending] == [False]
    await bridge.aclose()


@pytest.mark.parametrize("engine", ["classic", "live"])
def test_both_engines_apply_the_note_last(engine):
    """The announcement note is the outermost note on the backend prompt."""
    import inspect

    from openbase_coder_cli.livekit_agent import codex_llm, live_delegation

    source = inspect.getsource(
        codex_llm.CodexLLMStream._run_accepted_prompt
        if engine == "classic"
        else live_delegation.LiveDelegationBridge._run_delegation
    )
    assert source.index("apply_screen_context") < source.index(
        "apply_heard_announcements"
    )
