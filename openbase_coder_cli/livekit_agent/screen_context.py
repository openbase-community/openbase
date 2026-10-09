"""What the caller has on screen in the phone app, for the dispatcher.

A call always starts on the dispatcher, even from a project thread's chat
screen; routing the call into that thread is the explicit "Transfer call here"
action. So when the caller, looking at a project thread, says "subtract 38
from the result in this thread", the dispatcher must know which thread "this"
is (BUG 18, overnight iPhone QA 2026-10-09: the request reached the dispatcher
with no hint of the open thread and could not be forwarded).

The phone publishes the open conversation as the local participant attribute
``openbase.ui.focused_thread``: a JSON object ``{"thread_id", "name",
"directory"}`` for a project thread, the empty string for the dispatcher, a
new chat or any other screen. :class:`FocusedThreadTracker` follows it on the
room, and :func:`with_screen_context` puts a short note in front of the voice
prompt while speech goes to the dispatcher. The note uses the
``[Openbase system note: ...]`` envelope that chat displays already strip.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

FOCUSED_THREAD_ATTRIBUTE = "openbase.ui.focused_thread"
_MAX_FIELD_CHARS = 200
_AGENT_PARTICIPANT_KIND = 4  # livekit ParticipantKind.PARTICIPANT_KIND_AGENT


@dataclass(frozen=True)
class FocusedThread:
    thread_id: str
    name: str = ""
    directory: str = ""


def _clean(value: Any) -> str:
    text = " ".join(str(value or "").split())
    # Square brackets would unbalance the system-note envelope the chat
    # display strips; they carry nothing a thread name needs.
    text = text.replace("[", "(").replace("]", ")")
    return text[:_MAX_FIELD_CHARS]


def parse_focused_thread(value: str | None) -> FocusedThread | None:
    """The thread the attribute names, or None for anything else."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    thread_id = _clean(payload.get("thread_id"))
    if not thread_id:
        return None
    return FocusedThread(
        thread_id=thread_id,
        name=_clean(payload.get("name")),
        directory=_clean(payload.get("directory")),
    )


def screen_context_note(focus: FocusedThread) -> str:
    label = f'"{focus.name}"' if focus.name else "a project thread"
    where = f", folder {focus.directory}" if focus.directory else ""
    return (
        "[Openbase system note: the caller has the thread "
        f"{label} open in the Openbase phone app (thread id {focus.thread_id}"
        f'{where}) while speaking to you. When they say "this thread" or '
        "refer to what is on their screen, they mean that thread: continue "
        "it with super_agents_start_turn, or steer it with super_agents_steer "
        "if a turn is running, using that thread id, and relay its answer. "
        "Do not answer it from this conversation and do not start a new agent "
        "for it.]"
    )


def with_screen_context(
    prompt: str,
    focus: FocusedThread | None,
    *,
    dispatcher_thread_id: str | None = None,
) -> str:
    """``prompt`` with the screen note in front when a project thread is open.

    Nothing is added when no thread is open or when the open thread is the
    dispatcher's own conversation.
    """
    if focus is None or not focus.thread_id:
        return prompt
    if dispatcher_thread_id and focus.thread_id == dispatcher_thread_id:
        return prompt
    return f"{screen_context_note(focus)}\n\n{prompt}"


def _is_agent(participant: Any) -> bool:
    try:
        return int(getattr(participant, "kind", -1)) == _AGENT_PARTICIPANT_KIND
    except (TypeError, ValueError):
        return False


class FocusedThreadTracker:
    """Follows the caller's ``openbase.ui.focused_thread`` attribute on a room."""

    def __init__(self) -> None:
        self._room: Any = None
        self._focus: FocusedThread | None = None
        self._identity: str | None = None

    def current(self) -> FocusedThread | None:
        return self._focus

    def attach(self, room: Any) -> None:
        self._room = room
        for participant in (getattr(room, "remote_participants", None) or {}).values():
            self._observe(participant)
        room.on("participant_connected", self._observe)
        room.on("participant_attributes_changed", self._on_attributes_changed)
        room.on("participant_disconnected", self._on_participant_disconnected)

    def detach(self) -> None:
        room, self._room = self._room, None
        if room is None:
            return
        for event_name, handler in (
            ("participant_connected", self._observe),
            ("participant_attributes_changed", self._on_attributes_changed),
            ("participant_disconnected", self._on_participant_disconnected),
        ):
            room.off(event_name, handler)

    def _observe(self, participant: Any) -> None:
        if participant is None or _is_agent(participant):
            return
        attributes = getattr(participant, "attributes", None) or {}
        if FOCUSED_THREAD_ATTRIBUTE not in attributes:
            return
        self._update(
            parse_focused_thread(attributes.get(FOCUSED_THREAD_ATTRIBUTE)),
            str(getattr(participant, "identity", "") or ""),
        )

    def _update(self, focus: FocusedThread | None, identity: str) -> None:
        if focus != self._focus:
            logger.info(
                "dispatch_timing stage=screen_focus_changed thread_id=%s",
                focus.thread_id if focus else "",
            )
        self._focus = focus
        self._identity = identity or None

    def _on_attributes_changed(self, changed: dict, participant: Any) -> None:
        if FOCUSED_THREAD_ATTRIBUTE not in (changed or {}):
            return
        if _is_agent(participant):
            return
        self._update(
            parse_focused_thread(changed.get(FOCUSED_THREAD_ATTRIBUTE)),
            str(getattr(participant, "identity", "") or ""),
        )

    def _on_participant_disconnected(self, participant: Any) -> None:
        identity = str(getattr(participant, "identity", "") or "")
        if identity and identity == self._identity:
            self._update(None, "")


def apply_screen_context(voice_router: Any, prompt: str) -> str:
    """The dispatcher's voice prompt with the open-thread note, when one applies.

    Only speech going to the dispatcher gets the note: after a transfer the
    caller is already talking to that thread.
    """
    if not getattr(voice_router, "is_dispatcher_active", False):
        return prompt
    tracker = getattr(voice_router, "focused_thread_tracker", None)
    focus = tracker.current() if tracker is not None else None
    dispatcher = getattr(voice_router, "active_client", None)
    return with_screen_context(
        prompt,
        focus,
        dispatcher_thread_id=getattr(dispatcher, "_thread_id", None) or None,
    )
