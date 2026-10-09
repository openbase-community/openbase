from __future__ import annotations

import json
from types import SimpleNamespace

from openbase_coder_cli.livekit_agent import screen_context as sc
from openbase_coder_cli.livekit_agent.screen_context import (
    FOCUSED_THREAD_ATTRIBUTE,
    FocusedThread,
    FocusedThreadTracker,
    apply_screen_context,
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
