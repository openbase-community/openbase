from __future__ import annotations

import json
from types import SimpleNamespace

from openbase_coder_cli.livekit_agent import screen_context as sc
from openbase_coder_cli.livekit_agent.screen_context import (
    CALL_STATE_ATTRIBUTE,
    FOCUSED_THREAD_ATTRIBUTE,
    CallerAttributeTracker,
    CallState,
    FocusedThread,
    FocusedThreadTracker,
    apply_screen_context,
    call_state_clause,
    parse_call_state,
    parse_focused_thread,
    with_screen_context,
)

LIGHTHOUSE = {
    "thread_id": "s_4edd576829854b68b142367d698474f2",
    "name": "lighthouse-738",
    "directory": "/data/workspace/tic-tac-toe",
}


def test_parse_reads_a_project_thread_and_ignores_everything_else():
    focus = parse_focused_thread(json.dumps(LIGHTHOUSE))
    assert focus == FocusedThread(**LIGHTHOUSE)
    for value in ("", None, "not json", "[]", json.dumps({"name": "x"}), json.dumps({"thread_id": ""})):
        assert parse_focused_thread(value) is None


def test_parse_keeps_the_note_envelope_balanced():
    focus = parse_focused_thread(json.dumps({"thread_id": "s_1", "name": "fix [WIP] build]"}))
    assert focus.name == "fix (WIP) build)"
    note = sc.screen_context_note(focus)
    assert note.count("[") == note.count("]") == 1


def test_note_is_added_only_for_a_project_thread_other_than_the_dispatcher():
    focus = FocusedThread(**LIGHTHOUSE)
    prompt = "<voice>Subtract 38 in this thread</voice>"
    noted = with_screen_context(prompt, focus, dispatcher_thread_id="s_dispatcher")
    assert noted.startswith("[Openbase system note: the caller has the thread")
    assert noted.endswith(prompt)
    assert with_screen_context(prompt, None) == prompt
    assert with_screen_context(prompt, focus, dispatcher_thread_id=focus.thread_id) == prompt


def test_note_is_stripped_by_the_chat_display_sanitizer():
    from super_agents.claude_prompts import user_prompt_for_display

    focus = FocusedThread(**LIGHTHOUSE)
    prompt = with_screen_context("<voice>Answer just the number</voice>", focus)
    assert user_prompt_for_display(prompt) == "Answer just the number"


def test_apply_only_while_the_dispatcher_has_the_call():
    focus = FocusedThread(**LIGHTHOUSE)
    tracker = SimpleNamespace(current=lambda: focus)
    router = SimpleNamespace(
        is_dispatcher_active=True,
        focused_thread_tracker=tracker,
        active_client=SimpleNamespace(_thread_id="s_dispatcher"),
    )
    assert apply_screen_context(router, "<voice>x</voice>").startswith("[Openbase system note")
    router.is_dispatcher_active = False
    assert apply_screen_context(router, "<voice>x</voice>") == "<voice>x</voice>"
    router.is_dispatcher_active = True
    router.focused_thread_tracker = None
    assert apply_screen_context(router, "<voice>x</voice>") == "<voice>x</voice>"


class _Room:
    def __init__(self, participants=()):
        self.remote_participants = {p.identity: p for p in participants}
        self.handlers: dict[str, object] = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def off(self, event, handler):
        self.handlers.pop(event, None)


def _phone(attributes=None, kind=0):
    return SimpleNamespace(identity="phone", kind=kind, attributes=attributes or {})


def test_tracker_follows_the_phone_attribute_and_ignores_agents():
    phone = _phone({FOCUSED_THREAD_ATTRIBUTE: json.dumps(LIGHTHOUSE)})
    room = _Room([phone])
    tracker = FocusedThreadTracker()
    tracker.attach(room)
    assert tracker.current() == FocusedThread(**LIGHTHOUSE)

    changed = room.handlers["participant_attributes_changed"]
    changed({FOCUSED_THREAD_ATTRIBUTE: ""}, phone)
    assert tracker.current() is None

    agent = _phone(kind=4)
    changed({FOCUSED_THREAD_ATTRIBUTE: json.dumps(LIGHTHOUSE)}, agent)
    assert tracker.current() is None

    changed({FOCUSED_THREAD_ATTRIBUTE: json.dumps(LIGHTHOUSE)}, phone)
    assert tracker.current() == FocusedThread(**LIGHTHOUSE)
    changed({"other": "1"}, phone)
    assert tracker.current() == FocusedThread(**LIGHTHOUSE)

    room.handlers["participant_disconnected"](phone)
    assert tracker.current() is None

    tracker.detach()
    assert room.handlers == {}


def test_missing_call_state_invalidates_prior_notes_after_transfer():
    phone = _phone({CALL_STATE_ATTRIBUTE: json.dumps(EARPIECE)})
    room = _Room([phone])
    tracker = CallerAttributeTracker()
    tracker.attach(room)
    router = SimpleNamespace(is_dispatcher_active=False, focused_thread_tracker=tracker)
    prompt = "<voice>Am I still on speakerphone?</voice>"
    assert "speakerphone off (earpiece)" in apply_screen_context(router, prompt)
    room.handlers["participant_disconnected"](phone)
    unavailable = apply_screen_context(router, prompt)
    assert "Current call state is unavailable" in unavailable
    assert "do not reuse earlier call-state notes or guess" in unavailable
    assert "speakerphone off" not in unavailable
    from super_agents.claude_prompts import user_prompt_for_display

    assert user_prompt_for_display(unavailable) == "Am I still on speakerphone?"
    room.handlers["participant_connected"](phone)
    assert "speakerphone off (earpiece)" in apply_screen_context(router, prompt)
    room.handlers["participant_attributes_changed"]({CALL_STATE_ATTRIBUTE: "invalid"}, phone)
    assert "Current call state is unavailable" in apply_screen_context(router, prompt)


def test_tracker_reads_attributes_on_a_phone_that_joins_after_attach():
    room = _Room()
    tracker = FocusedThreadTracker()
    tracker.attach(room)
    handler = room.handlers.get("participant_connected")
    assert handler is not None
    handler(_phone({FOCUSED_THREAD_ATTRIBUTE: json.dumps(LIGHTHOUSE)}))
    assert tracker.current() == FocusedThread(**LIGHTHOUSE)
    tracker.detach()
    assert room.handlers == {}


# --- call state (forensics F2/F3, staging call 2026-10-09) -------------------

EARPIECE = {"muted": False, "speaker": False, "route": "earpiece", "auto_muted": False}


def test_parse_call_state_reads_the_contract_and_degrades_unknown_fields():
    assert parse_call_state(json.dumps(EARPIECE)) == CallState(False, False, "earpiece", False)
    speaker = parse_call_state(json.dumps({"muted": True, "speaker": True, "route": "SPEAKER", "auto_muted": True}))
    assert speaker == CallState(muted=True, speaker=True, route="speaker", auto_muted=True)
    # Unknown or missing route and auto_muted degrade instead of failing.
    assert parse_call_state(json.dumps({"muted": False, "speaker": True})) == CallState(False, True)
    assert parse_call_state(json.dumps({"muted": False, "speaker": False, "route": "carplay"})).route == "unknown"
    # auto_muted only means something while muted.
    assert parse_call_state(json.dumps({"muted": False, "speaker": False, "auto_muted": True})).auto_muted is False


def test_parse_call_state_rejects_anything_that_is_not_a_real_state():
    for value in (
        "", None, "not json", "[]", "{}",
        json.dumps({"muted": "false", "speaker": False}),
        json.dumps({"muted": False, "speaker": 1}),
        json.dumps({"speaker": False}),
    ):
        assert parse_call_state(value) is None, value


def test_call_state_clause_is_compact_and_truthful():
    assert call_state_clause(CallState(False, False, "earpiece")) == (
        "Call state: microphone on, speakerphone off (earpiece)."
    )
    assert call_state_clause(CallState(False, True, "speaker")) == (
        "Call state: microphone on, speakerphone on."
    )
    assert call_state_clause(CallState(True, False, "bluetooth")) == (
        "Call state: microphone muted, speakerphone off (Bluetooth audio)."
    )
    assert call_state_clause(CallState(True, False, "wired", auto_muted=True)) == (
        "Call state: microphone auto-muted while the agent works, speakerphone off (wired headset)."
    )
    assert call_state_clause(CallState(False, False, "unknown")) == (
        "Call state: microphone on, speakerphone off."
    )


def test_note_carries_the_call_state_with_or_without_an_open_thread():
    prompt = "<voice>Are you on speakerphone?</voice>"
    state = CallState(False, False, "earpiece")
    alone = with_screen_context(prompt, None, call_state=state)
    assert alone.startswith("[Openbase system note: Call state: microphone on, speakerphone off (earpiece).")
    assert "never from a guess.]" in alone
    assert alone.endswith(prompt)
    assert alone.count("[") == alone.count("]") == 1

    both = with_screen_context(prompt, FocusedThread(**LIGHTHOUSE), call_state=state)
    assert both.startswith("[Openbase system note: the caller has the thread")
    assert "Call state: microphone on, speakerphone off (earpiece)." in both
    assert both.count("[Openbase system note:") == 1
    # The dispatcher's own thread still drops the focus part, not the state.
    own = with_screen_context(
        prompt, FocusedThread(**LIGHTHOUSE), dispatcher_thread_id=LIGHTHOUSE["thread_id"], call_state=state
    )
    assert own == alone
    assert with_screen_context(prompt, None, call_state=None) == prompt


def test_call_state_note_is_stripped_by_the_chat_display_sanitizer():
    from super_agents.claude_prompts import user_prompt_for_display

    prompt = with_screen_context(
        "<voice>Are you on speakerphone?</voice>",
        FocusedThread(**LIGHTHOUSE),
        call_state=CallState(True, True, "speaker", auto_muted=True),
    )
    assert user_prompt_for_display(prompt) == "Are you on speakerphone?"


def test_apply_sends_the_call_state_to_every_route_and_the_focus_to_the_dispatcher():
    state = CallState(False, True, "speaker")
    tracker = SimpleNamespace(current=lambda: FocusedThread(**LIGHTHOUSE), call_state=lambda: state)
    router = SimpleNamespace(
        is_dispatcher_active=True,
        focused_thread_tracker=tracker,
        active_client=SimpleNamespace(_thread_id="s_dispatcher"),
    )
    on_dispatcher = apply_screen_context(router, "<voice>x</voice>")
    assert "the caller has the thread" in on_dispatcher
    assert "speakerphone on." in on_dispatcher
    router.is_dispatcher_active = False
    on_thread = apply_screen_context(router, "<voice>x</voice>")
    assert on_thread.startswith("[Openbase system note: Call state: microphone on, speakerphone on.")
    assert "the caller has the thread" not in on_thread
    assert on_thread.endswith("<voice>x</voice>")
    # A tracker that only knows the focus (older stubs) still works.
    router.focused_thread_tracker = SimpleNamespace(current=lambda: FocusedThread(**LIGHTHOUSE))
    assert apply_screen_context(router, "<voice>x</voice>") == "<voice>x</voice>"


def test_tracker_follows_both_attributes_and_clears_each_on_its_publisher_leaving():
    phone = _phone({
        FOCUSED_THREAD_ATTRIBUTE: json.dumps(LIGHTHOUSE),
        CALL_STATE_ATTRIBUTE: json.dumps(EARPIECE),
    })
    room = _Room([phone])
    tracker = CallerAttributeTracker()
    assert FocusedThreadTracker is CallerAttributeTracker
    tracker.attach(room)
    assert tracker.current() == FocusedThread(**LIGHTHOUSE)
    assert tracker.call_state() == CallState(False, False, "earpiece")

    changed = room.handlers["participant_attributes_changed"]
    # One attribute changing leaves the other alone.
    changed({CALL_STATE_ATTRIBUTE: json.dumps({**EARPIECE, "muted": True, "auto_muted": True})}, phone)
    assert tracker.call_state() == CallState(True, False, "earpiece", auto_muted=True)
    assert tracker.current() == FocusedThread(**LIGHTHOUSE)
    changed({FOCUSED_THREAD_ATTRIBUTE: ""}, phone)
    assert tracker.current() is None
    assert tracker.call_state() == CallState(True, False, "earpiece", auto_muted=True)
    # An unreadable state clears it rather than keeping a stale one.
    changed({CALL_STATE_ATTRIBUTE: "garbage"}, phone)
    assert tracker.call_state() is None
    # Agents never publish caller state.
    changed({CALL_STATE_ATTRIBUTE: json.dumps(EARPIECE)}, _phone(kind=4))
    assert tracker.call_state() is None

    # A second participant's state belongs to it: the first leaving keeps it.
    watch = SimpleNamespace(identity="watch", kind=0, attributes={})
    changed({CALL_STATE_ATTRIBUTE: json.dumps(EARPIECE)}, watch)
    changed({FOCUSED_THREAD_ATTRIBUTE: json.dumps(LIGHTHOUSE)}, phone)
    room.handlers["participant_disconnected"](phone)
    assert tracker.current() is None
    assert tracker.call_state() == CallState(False, False, "earpiece")
    room.handlers["participant_disconnected"](watch)
    assert tracker.call_state() is None

    tracker.detach()
    assert room.handlers == {}
