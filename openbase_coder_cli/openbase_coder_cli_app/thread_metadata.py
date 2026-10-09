from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from super_agents.state import as_session_record_map

from openbase_coder_cli.dispatcher_config import selected_tts_provider_id
from openbase_coder_cli.json_snapshot import JsonFileSnapshot
from openbase_coder_cli.livekit_voice_history import (
    VoiceHistoryEntry,
    get_voice_history_entry,
)
from openbase_coder_cli.livekit_voice_route import (
    get_livekit_voice_route_state,
    super_agent_voice_for_context,
)
from openbase_coder_cli.openbase_coder_cli_app.item_tags import thread_tags
from openbase_coder_cli.openbase_coder_cli_app.thread_favorites import favorite_payload
from openbase_coder_cli.thread_model_overrides import get_thread_model_override
from openbase_coder_cli.thread_sync.thread_moves import moved_to_payload
from openbase_coder_cli.tts_providers import voice_name_for_id

VoiceRouteRole = Literal["none", "dispatcher", "active_target"]
SUPER_AGENTS_STATE_FILE_ENV = "SUPER_AGENTS_STATE_FILE"


def get_livekit_shared_thread_id() -> str | None:
    state = get_livekit_voice_route_state()
    return state.dispatcher_thread_id


def annotate_thread_payload(
    payload: dict[str, Any],
    *,
    thread_id: str | None = None,
) -> dict[str, Any]:
    resolved_thread_id = thread_id or str(
        payload.get("thread_id") or payload.get("session_id") or ""
    )
    route_state = get_livekit_voice_route_state()
    is_dispatcher = bool(
        resolved_thread_id
        and route_state.dispatcher_thread_id
        and resolved_thread_id == route_state.dispatcher_thread_id
    )
    is_active_target = bool(
        resolved_thread_id
        and route_state.active_target_thread_id
        and resolved_thread_id == route_state.active_target_thread_id
    )

    role: VoiceRouteRole = "none"
    if is_active_target:
        role = "active_target"
    elif is_dispatcher:
        role = "dispatcher"

    display_name = "dispatcher" if is_dispatcher else _thread_display_name(payload)
    agent_name = _thread_agent_name(payload) or _super_agents_agent_name(
        resolved_thread_id
    )
    assignment = None

    if is_dispatcher:
        agent_name = "dispatcher"
        assignment = _voice_assignment_payload(
            thread_id=resolved_thread_id,
            agent_name=agent_name,
            voice_id=route_state.dispatcher_voice_id,
            voice_name=route_state.dispatcher_voice_name,
            source="route_state",
        )
    elif is_active_target:
        active_agent_name = route_state.active_target_voice_name or agent_name
        voice_name = (
            voice_name_for_id(
                selected_tts_provider_id(), route_state.active_target_voice_id
            )
            or route_state.active_target_voice_name
        )
        assignment = _voice_assignment_payload(
            thread_id=resolved_thread_id,
            agent_name=active_agent_name,
            voice_id=route_state.active_target_voice_id,
            voice_name=voice_name,
            source="route_state",
        )
    else:
        history = get_voice_history_entry(resolved_thread_id)
        if history is not None:
            assignment = _voice_assignment_from_history(history)
        else:
            # Derive a stable voice from the thread context even when no
            # agent_name is set. Super Agent / dispatcher-spawned threads carry
            # a label but almost never an agent_name (it stays None in the
            # Super Agents state), so gating derivation on agent_name left those
            # threads nameless in the console. super_agent_voice_for_context
            # falls back to a deterministic voice keyed on thread_id/label, so
            # every non-dispatcher thread now shows a consistent human name
            # (e.g. "Jacqueline") whether or not it was ever on a live call.
            voice = super_agent_voice_for_context(
                resolved_thread_id,
                display_name,
                agent_name,
            )
            if voice is not None:
                assignment = _voice_assignment_payload(
                    thread_id=resolved_thread_id,
                    agent_name=agent_name,
                    voice_id=voice.voice_id,
                    voice_name=voice.name,
                    source="derived",
                )

    favorite = favorite_payload(resolved_thread_id)
    return {
        **payload,
        "agent_name": agent_name,
        "display_name": display_name,
        "is_favorite": favorite["is_favorite"],
        "favorited_at": favorite["favorited_at"],
        "model_override": get_thread_model_override(resolved_thread_id),
        # Set while the thread is being pushed to (or has moved to) another
        # computer; this copy is then read-only.
        "moved_to": moved_to_payload(resolved_thread_id),
        "tags": thread_tags(resolved_thread_id),
        "voice_route": {
            "role": role,
            "active": is_active_target
            or (is_dispatcher and not route_state.active_target_thread_id),
        },
        "voice_assignment": assignment,
    }


def _thread_display_name(payload: dict[str, Any]) -> str:
    for key in ("name", "title", "preview"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    directory = payload.get("directory")
    if isinstance(directory, str) and directory.strip():
        basename = directory.rstrip("/").rsplit("/", 1)[-1]
        if basename:
            return basename
    # Never the thread id: display names are titles, not identifiers.
    return "Conversation"


def _thread_agent_name(payload: dict[str, Any]) -> str | None:
    value = payload.get("agent_name") or payload.get("agentName")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _super_agents_agent_name(thread_id: str | None) -> str | None:
    if not thread_id:
        return None
    return _state_agent_names.get(_super_agents_state_path()).get(thread_id)


def _parse_state_agent_names(raw: Any) -> dict[str, str]:
    """thread id -> agent name, from one version of the Super Agents state file.

    The state file is a few hundred KB and is written atomically, so parsing
    it once per on-disk version (instead of under its file lock for every
    thread annotated) keeps thread-list annotation off the parse cost.
    """
    sessions = as_session_record_map(
        raw.get("sessions") if isinstance(raw, dict) else None
    )
    return {
        thread_id: session.agent_name
        for thread_id, session in sessions.items()
        if session.agent_name
    }


_state_agent_names: JsonFileSnapshot[dict[str, str]] = JsonFileSnapshot(
    _parse_state_agent_names
)


def _super_agents_state_path() -> Path:
    configured = os.environ.get(SUPER_AGENTS_STATE_FILE_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".super-agents" / "state.json"


def _voice_assignment_from_history(history: VoiceHistoryEntry) -> dict[str, str | None]:
    return _voice_assignment_payload(
        thread_id=history.thread_id,
        agent_name=history.agent_name,
        voice_id=history.voice_id,
        voice_name=history.voice_name,
        source=history.source,
    )


def _voice_assignment_payload(
    *,
    thread_id: str,
    agent_name: str | None,
    voice_id: str | None,
    voice_name: str | None,
    source: str,
) -> dict[str, str | None] | None:
    if not voice_id and not voice_name and not agent_name:
        return None
    return {
        "thread_id": thread_id,
        "agent_name": agent_name,
        "voice_id": voice_id,
        "voice_name": voice_name,
        "source": source,
    }
