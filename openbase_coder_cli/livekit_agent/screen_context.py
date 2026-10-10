"""What the caller has on screen and how their call is set up, for the agent.

A call always starts on the dispatcher, even from a project thread's chat
screen; routing the call into that thread is the explicit "Transfer call here"
action. So when the caller, looking at a project thread, says "subtract 38
from the result in this thread", the dispatcher must know which thread "this"
is (BUG 18, overnight iPhone QA 2026-10-09: the request reached the dispatcher
with no hint of the open thread and could not be forwarded).

The phone publishes two local participant attributes:

- ``openbase.ui.focused_thread``: the open conversation as a JSON object
  ``{"thread_id", "name", "directory"}`` for a project thread, the empty
  string for the dispatcher, a new chat or any other screen.
- ``openbase.call.state``: the call controls as a JSON object ``{"muted",
  "speaker", "route", "auto_muted"}`` (``route`` is one of speaker, earpiece,
  bluetooth, wired, unknown), republished on every change and at join. Without
  it the agent could only guess at "are you on speakerphone?" (forensics
  F2/F3, staging call 2026-10-09: the dispatcher invented a speaker state).

:class:`CallerAttributeTracker` follows both on the room, and
:func:`with_screen_context` puts a short note in front of the voice prompt:
the call state for every route (a thread answers the caller directly after a
transfer), the open thread only while speech goes to the dispatcher. The note
uses the ``[Openbase system note: ...]`` envelope that chat displays already
strip.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

FOCUSED_THREAD_ATTRIBUTE = "openbase.ui.focused_thread"
CALL_STATE_ATTRIBUTE = "openbase.call.state"
CALL_STATE_ROUTES = frozenset({"speaker", "earpiece", "bluetooth", "wired", "unknown"})
_MAX_FIELD_CHARS = 200
_AGENT_PARTICIPANT_KIND = 4  # livekit ParticipantKind.PARTICIPANT_KIND_AGENT


@dataclass(frozen=True)
class FocusedThread:
    thread_id: str
    name: str = ""
    directory: str = ""


@dataclass(frozen=True)
class CallState:
    """The caller's call controls as the phone last published them."""

    muted: bool
    speaker: bool
    route: str = "unknown"
    auto_muted: bool = False


def _clean(value: Any) -> str:
    text = " ".join(str(value or "").split())
    # Square brackets would unbalance the system-note envelope the chat
    # display strips; they carry nothing a thread name needs.
    text = text.replace("[", "(").replace("]", ")")
    return text[:_MAX_FIELD_CHARS]


def _json_object(value: str | None) -> dict | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_focused_thread(value: str | None) -> FocusedThread | None:
    """The thread the attribute names, or None for anything else."""
    payload = _json_object(value)
    if payload is None:
        return None
    thread_id = _clean(payload.get("thread_id"))
    if not thread_id:
        return None
    return FocusedThread(
        thread_id=thread_id,
        name=_clean(payload.get("name")),
        directory=_clean(payload.get("directory")),
    )


def parse_call_state(value: str | None) -> CallState | None:
    """The call state the attribute carries, or None when it says nothing usable.

    ``muted`` and ``speaker`` must be real booleans: a state the phone could
    not determine is better left out than reported wrong. ``route`` outside
    the contract and a missing ``auto_muted`` degrade to unknown / False.
    """
    payload = _json_object(value)
    if payload is None:
        return None
    muted = payload.get("muted")
    speaker = payload.get("speaker")
    if not isinstance(muted, bool) or not isinstance(speaker, bool):
        return None
    route = str(payload.get("route") or "").strip().lower()
    if route not in CALL_STATE_ROUTES:
        route = "unknown"
    auto_muted = payload.get("auto_muted")
    return CallState(
        muted=muted,
        speaker=speaker,
        route=route,
        auto_muted=auto_muted is True and muted,
    )


_ROUTE_DETAIL = {
    "earpiece": " (earpiece)",
    "bluetooth": " (Bluetooth audio)",
    "wired": " (wired headset)",
}


def call_state_clause(state: CallState) -> str:
    """One compact clause, e.g. ``microphone on, speakerphone off (earpiece)``."""
    if not state.muted:
        microphone = "microphone on"
    elif state.auto_muted:
        microphone = "microphone auto-muted while the agent works"
    else:
        microphone = "microphone muted"
    if state.speaker:
        output = "speakerphone on"
    else:
        output = "speakerphone off" + _ROUTE_DETAIL.get(state.route, "")
    return f"Call state: {microphone}, {output}."


def screen_context_note(
    focus: FocusedThread | None, call_state: CallState | None = None
) -> str:
    """The system note; empty when there is nothing to say."""
    parts: list[str] = []
    if focus is not None:
        label = f'"{focus.name}"' if focus.name else "a project thread"
        where = f", folder {focus.directory}" if focus.directory else ""
        parts.append(
            "the caller has the thread "
            f"{label} open in the Openbase phone app (thread id {focus.thread_id}"
            f'{where}) while speaking to you. When they say "this thread" or '
            "refer to what is on their screen, they mean that thread: continue "
            "it with super_agents_start_turn using that thread name (it steers a "
            "running turn or starts the next one); the thread id is for "
            "super_agents_read or super_agents_steer. Relay its answer. Do not "
            "answer it from this conversation and do not start a new agent for "
            "it."
        )
    if call_state is not None:
        parts.append(
            call_state_clause(call_state)
            + " Answer questions about mute, speakerphone or the audio route "
            "from this, never from a guess."
        )
    if not parts:
        return ""
    return "[Openbase system note: " + " ".join(parts) + "]"


def with_screen_context(
    prompt: str,
    focus: FocusedThread | None,
    *,
    dispatcher_thread_id: str | None = None,
    call_state: CallState | None = None,
) -> str:
    """``prompt`` with the system note in front, when there is one.

    The open-thread part is left out when no thread is open or when the open
    thread is the dispatcher's own conversation; the call-state part whenever
    the phone has published one.
    """
    if focus is not None and (
        not focus.thread_id
        or (dispatcher_thread_id and focus.thread_id == dispatcher_thread_id)
    ):
        focus = None
    note = screen_context_note(focus, call_state)
    if not note:
        return prompt
    return f"{note}\n\n{prompt}"


def _is_agent(participant: Any) -> bool:
    try:
        return int(getattr(participant, "kind", -1)) == _AGENT_PARTICIPANT_KIND
    except (TypeError, ValueError):
        return False


class CallerAttributeTracker:
    """Follows the caller's participant attributes on a room.

    One tracker serves every attribute the phone publishes; each is parsed by
    its own function and cleared when the participant that published it
    leaves (a reconnecting phone rejoins and republishes).
    """

    _PARSERS: dict[str, Callable[[str | None], Any]] = {
        FOCUSED_THREAD_ATTRIBUTE: parse_focused_thread,
        CALL_STATE_ATTRIBUTE: parse_call_state,
    }

    def __init__(self) -> None:
        self._room: Any = None
        self._values: dict[str, Any] = {}
        self._identities: dict[str, str] = {}

    def current(self) -> FocusedThread | None:
        """The open project thread (the original focused-thread API)."""
        return self._values.get(FOCUSED_THREAD_ATTRIBUTE)

    focused_thread = current

    def call_state(self) -> CallState | None:
        return self._values.get(CALL_STATE_ATTRIBUTE)

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
        self._apply(attributes, participant)

    def _apply(self, attributes: dict, participant: Any) -> None:
        identity = str(getattr(participant, "identity", "") or "")
        for name, parse in self._PARSERS.items():
            if name in attributes:
                self._update(name, parse(attributes.get(name)), identity)

    def _update(self, name: str, value: Any, identity: str) -> None:
        if value != self._values.get(name):
            if name == FOCUSED_THREAD_ATTRIBUTE:
                logger.info(
                    "dispatch_timing stage=screen_focus_changed thread_id=%s",
                    value.thread_id if value else "",
                )
            else:
                logger.info(
                    "dispatch_timing stage=call_state_changed state=%s",
                    call_state_clause(value) if value else "",
                )
        self._values[name] = value
        if identity:
            self._identities[name] = identity
        else:
            self._identities.pop(name, None)

    def _on_attributes_changed(self, changed: dict, participant: Any) -> None:
        if not changed or _is_agent(participant):
            return
        self._apply(changed, participant)

    def _on_participant_disconnected(self, participant: Any) -> None:
        identity = str(getattr(participant, "identity", "") or "")
        if not identity:
            return
        for name, owner in list(self._identities.items()):
            if owner == identity:
                self._update(name, None, "")


# The original name, kept for callers and tests that only need the focus.
FocusedThreadTracker = CallerAttributeTracker


def apply_screen_context(voice_router: Any, prompt: str) -> str:
    """The voice prompt with the system note, when one applies.

    The call state goes to whichever route has the call: after a transfer the
    thread answers the caller directly and must not guess at it either. The
    open-thread part only goes to the dispatcher: after a transfer the caller
    is already talking to that thread.
    """
    tracker = getattr(voice_router, "focused_thread_tracker", None)
    if tracker is None:
        return prompt
    call_state_of = getattr(tracker, "call_state", None)
    call_state = call_state_of() if callable(call_state_of) else None
    if not getattr(voice_router, "is_dispatcher_active", False):
        return with_screen_context(prompt, None, call_state=call_state)
    dispatcher = getattr(voice_router, "active_client", None)
    return with_screen_context(
        prompt,
        tracker.current(),
        dispatcher_thread_id=getattr(dispatcher, "_thread_id", None) or None,
        call_state=call_state,
    )
