"""Keep Openbase's speaking identity in the backend's ordinary agent metadata.

The same stable catalog assignment feeds the UI, backend tools and prompts.
This adapter lives in Openbase; standalone Super Agents has no voice policy.
"""

from __future__ import annotations

from typing import Any

from openbase_coder_cli.livekit_agent.codex_turns import (
    _with_super_agent_identity_instructions,
)
from openbase_coder_cli.livekit_voice_route import (
    is_dispatcher_identity,
    super_agent_voice_for_context,
)

from .models import ThreadInfo


async def ensure_speaking_identity(client: Any, thread: ThreadInfo) -> ThreadInfo:
    if thread.agent_name:
        return thread
    voice = super_agent_voice_for_context(thread.session_id, thread.name)
    if voice is None:
        return thread
    name = (
        "dispatcher"
        if is_dispatcher_identity(thread.session_id, thread.name)
        else voice.name
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
            return thread
        # Codex's generic turn adapter incorporates stored agentName into
        # developer instructions, including queued and resumed turns.
        await merge(thread.session_id, {"agentName": name})
    thread.agent_name = name
    return thread
