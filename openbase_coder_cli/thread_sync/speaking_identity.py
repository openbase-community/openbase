"""Keep Openbase's speaking identity in the backend's ordinary agent metadata.

The same stable catalog assignment feeds the UI, backend tools and prompts.
This adapter lives in Openbase; standalone Super Agents has no voice policy.
"""

from __future__ import annotations

import time
from typing import Any

from openbase_coder_cli.livekit_agent.codex_turns import (
    _with_super_agent_identity_instructions,
)
from openbase_coder_cli.livekit_voice_history import get_voice_history_entry
from openbase_coder_cli.livekit_voice_route import (
    is_dispatcher_identity,
    super_agent_voice_for_context,
)

from .models import ThreadInfo

# Thread lists call this for every thread on every poll. Resolving reads the
# voice route and the whole Super Agents state file (megabytes of JSON), which
# pinned the API's CPU under phone polling, so each thread's outcome is reused
# for a short window. A name changed elsewhere shows up within that window.
IDENTITY_RECHECK_SECONDS = 30.0
_resolved_agent_names: dict[str, tuple[float, str]] = {}
_no_voice_checked_at: dict[str, float] = {}


def _remember(session_id: str, name: str) -> None:
    _resolved_agent_names[session_id] = (time.monotonic(), name)


async def ensure_speaking_identity(client: Any, thread: ThreadInfo) -> ThreadInfo:
    if thread.agent_name:
        return thread
    now = time.monotonic()
    cached = _resolved_agent_names.get(thread.session_id)
    if cached is not None and now - cached[0] < IDENTITY_RECHECK_SECONDS:
        thread.agent_name = cached[1]
        return thread
    checked_at = _no_voice_checked_at.get(thread.session_id)
    if checked_at is not None and now - checked_at < IDENTITY_RECHECK_SECONDS:
        return thread
    voice = super_agent_voice_for_context(thread.session_id, thread.name)
    if voice is None:
        _no_voice_checked_at[thread.session_id] = now
        return thread
    _no_voice_checked_at.pop(thread.session_id, None)
    history = get_voice_history_entry(thread.session_id)
    # Older calls recorded their character before the backend carried an
    # agentName. Keep that assignment, just as the UI and transfer resolver do.
    recorded_name = (history.agent_name or history.voice_name) if history else None
    name = (
        "dispatcher"
        if is_dispatcher_identity(thread.session_id, thread.name)
        else recorded_name or voice.name
    )
    # Claude's public store preserves custom instructions while its SDK
    # prompt builder reads the persisted agent_name on every turn.
    store = getattr(client, "store", None)
    if store is not None:
        record = store.get_session(thread.session_id)
        name = record.agent_name or name
        if not record.agent_name:
            store.update_session(
                thread.session_id,
                agent_name=name,
                updated_at=record.updated_at,
                developer_instructions=_with_super_agent_identity_instructions(
                    record.developer_instructions, record.name, name
                ),
            )
    else:
        merge = getattr(client, "merge_session", None)
        if not callable(merge):
            return thread
        get_session = getattr(client, "get_session", None)
        record = await get_session(thread.session_id) if callable(get_session) else None
        if record is not None and record.agent_name:
            thread.agent_name = record.agent_name
            _remember(thread.session_id, record.agent_name)
            return thread
        # Codex's generic turn adapter incorporates stored agentName into
        # developer instructions, including queued and resumed turns.
        await merge(thread.session_id, {"agentName": name})
    thread.agent_name = name
    _remember(thread.session_id, name)
    return thread
