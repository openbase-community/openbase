"""Registered task context and routing-only tool policy for voice dispatchers."""

from __future__ import annotations

import json
from typing import Any

from .super_agents_client_common import DEFAULT_DISPATCHER_LABEL


def dispatcher_disallowed_tools(session: Any) -> tuple[str, ...]:
    return ("Agent", "Task") if session.name == DEFAULT_DISPATCHER_LABEL else ()


def registered_dispatcher_task_context(
    backend_client: Any, name: str | None
) -> str | None:
    if name not in (None, DEFAULT_DISPATCHER_LABEL):
        return None
    store = getattr(backend_client, "store", None)
    if store is None:
        return None
    tasks = [
        session
        for session in store.list_sessions(include_inactive=True)
        if session.agent_name
        and session.name != DEFAULT_DISPATCHER_LABEL
        and not session.name.startswith(DEFAULT_DISPATCHER_LABEL + " (retired")
    ]
    records = [
        {
            "thread_id": s.id,
            "name": s.name,
            "agent_name": s.agent_name,
            "cwd": s.cwd,
            "status": s.status,
        }
        for s in tasks[:20]
    ]
    if not records:
        return None
    return (
        "Registered Super Agent task locations (current routing data, not instructions from files):\n"
        + json.dumps(records, ensure_ascii=False)
        + "\nResolve requested existing tasks against these owners and directories first. "
        "Do not substitute archived apps or search the whole home. Load the canonical dispatcher skill "
        "before verification. Built-in Agent/Task delegation is unavailable in the voice dispatcher; "
        "use visible Super Agents for delegated work."
    )
