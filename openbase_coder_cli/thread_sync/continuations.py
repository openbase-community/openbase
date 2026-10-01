"""Create linked conversations across engines using supported backend APIs."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid

from super_agents.app_models import LabelQueryInput
from super_agents.app_protocol import extract_thread_id
from super_agents.initial_context import initialize_session_context
from super_agents.session_history import read_session_messages

from openbase_coder_cli.livekit_voice_route import get_livekit_voice_route_state

from . import continuation_store as store
from .continuation_context import build_context, combine_messages
from .session_manager_base import load_super_agent_developer_instructions

BACKENDS = {"codex": "Codex", "claude_code": "Claude Code"}


async def readiness(target, backend: str) -> str | None:
    if target is None:
        return "Enable this backend in Settings on this computer."
    if backend == "claude_code":
        from openbase_coder_cli.claude_auth import claude_auth_status

        try:
            sdk = target._client._sdk_loader()
        except ImportError:
            return "Install the Claude Agent SDK to enable conversation transfer."
        if not callable(getattr(sdk, "get_session_messages", None)):
            return "Update the Claude Agent SDK to enable conversation transfer."
        auth = await asyncio.to_thread(claude_auth_status)
        return (
            None if auth.logged_in else "Sign in to Claude Code on this computer first."
        )
    try:
        await target._client.ensure_connected()
        result = await target._client.request("account/read", {"refreshToken": False})
    except RuntimeError:
        return "Codex is unavailable. Check its service and sign-in."
    if result.get("requiresOpenaiAuth", True) and not result.get("account"):
        return "Sign in to Codex on this computer first."
    return None


def backend_manager(manager, backend: str):
    resolver = getattr(manager, "manager_for_backend", None)
    target = (
        resolver(backend)
        if resolver
        else (manager if manager._execution_backend == backend else None)
    )
    return target if target and target._client.backend == backend else None


async def options(manager, thread_id: str) -> dict:
    thread = await manager.get_thread_state(thread_id)
    if thread is None:
        raise ValueError("Thread not found.")
    reason = await blocked_reason(manager, thread)
    current = thread.backend or getattr(manager, "_execution_backend", None)
    availability = {
        backend: await readiness(backend_manager(manager, backend), backend)
        for backend in BACKENDS
        if backend != current and not reason
    }
    return {
        "options": [
            {
                "backend": backend,
                "label": label,
                "current": current == backend,
                "reason": reason
                or (
                    "Current backend"
                    if current == backend
                    else availability.get(backend)
                ),
            }
            for backend, label in BACKENDS.items()
        ]
    }


async def blocked_reason(manager, thread) -> str | None:
    route = get_livekit_voice_route_state()
    if (
        thread.session_id == route.dispatcher_thread_id
        or (thread.name or "").casefold() == "dispatcher"
    ):
        return "The dispatcher cannot switch backends here."
    if thread.session_id == route.active_target_thread_id:
        return "Leave this thread's voice call before switching."
    if thread.status in {"running", "waiting"}:
        return "Finish or stop the current turn before switching."
    if thread.queued_turns:
        return "Clear queued prompts before switching."
    approvals = await manager.list_approval_requests()
    if any(
        (item.get("thread_id") or item.get("threadId")) == thread.session_id
        for item in approvals
    ):
        return "Resolve the pending approval before switching."
    return None


def _boundary(thread):
    return (
        thread.updated_at,
        thread.status,
        thread.current_run.run_id if thread.current_run else None,
    )


async def create_continuation(
    manager, source_id: str, backend: str, operation_id: str
) -> dict:
    if backend not in BACKENDS:
        raise ValueError("Choose Codex or Claude Code.")
    operation_id = str(uuid.UUID(operation_id))
    target = backend_manager(manager, backend)
    if target is None:
        raise ValueError("The destination backend is not configured on this computer.")
    record, created = store.reserve(operation_id, source_id, backend)
    if not created:
        if record["state"] == "ready":
            return response(record)
        if record["state"] == "created":
            return await finish(record, target)
        raise RuntimeError(
            record.get("error")
            or "This switch is still preparing or its result is uncertain. Refresh before retrying."
        )
    try:
        source = await manager.get_thread_state(source_id)
        if source is None:
            raise ValueError("Source thread not found.")
        if reason := await blocked_reason(manager, source):
            raise ValueError(reason)
        source_backend = source.backend or getattr(manager, "_execution_backend", None)
        if source_backend not in BACKENDS or source_backend == backend:
            raise ValueError("Choose a different local backend.")
        source_manager = backend_manager(manager, source_backend)
        if source_manager is None:
            raise ValueError("Source backend is not configured on this computer.")
        if reason := await readiness(target, backend):
            raise ValueError(reason)
        messages = await read_session_messages(source_manager._client, source_id)
        previous = store.for_destination(source_id)
        messages = combine_messages(
            previous.get("messages", []) if previous else [], messages
        )
        source_name = source.name or source.title or source.preview or "Thread"
        base = (
            previous["base_name"]
            if previous and source_name == previous["name"]
            else source_name
        )
        name = f"{base} · {BACKENDS[backend]}"
        # Claude's generic store requires unique names. Never retire/rename
        # an existing conversation to make space for this continuation.
        if backend == "claude_code":
            index = 2
            while target._client.store.get_by_name(name):
                name = f"{base} ({index}) · {BACKENDS[backend]}"
                index += 1
        context, omitted = build_context(messages, source_name)
        fresh = await manager.get_thread_state(source_id)
        if fresh is None or _boundary(fresh) != _boundary(source):
            raise ValueError(
                "The source conversation changed. Wait for it to finish, then switch again."
            )
        if reason := await blocked_reason(manager, fresh):
            raise ValueError(reason)
        record = store.update(
            operation_id,
            state="creating",
            source_name=source_name,
            base_name=base,
            name=name,
            messages=messages,
            context=context,
            omitted=omitted,
            directory=source.directory,
            source_boundary=str(source.updated_at),
        )
        payload = {
            # Use an operation-specific native label during creation so the
            # Claude client's reuse-by-name behavior cannot touch another
            # conversation when two switches choose the same display name.
            "name": f"continuation-{operation_id}",
            "cwd": source.directory,
            **target._codex_permission_defaults(),
        }
        if instructions := load_super_agent_developer_instructions():
            payload["developerInstructions"] = instructions
        # Each backend client chooses its own compatible configured model.
        result = await target._client.start_thread(payload)
        destination_id = extract_thread_id(result)
        if not destination_id:
            session = result.get("session", {})
            destination_id = session.get("id")
        if not destination_id:
            raise RuntimeError("The backend did not return a destination thread ID.")
        record = store.update(
            operation_id, state="created", destination_id=destination_id
        )
        return await finish(record, target)
    except (ValueError, RuntimeError, OSError, asyncio.TimeoutError) as exc:
        # Persist the failure so retries cannot accidentally create twice.
        stage = record["state"]
        store.update(
            operation_id,
            state="created" if stage == "created" else "failed",
            error=str(exc),
            safe_to_retry=stage == "preparing",
        )
        raise


async def finish(record: dict, target) -> dict:
    from openbase_coder_cli.openbase_coder_cli_app.thread_origins import (
        MANUAL_ORIGIN,
        set_thread_origin,
    )

    await initialize_session_context(
        target._client, record["destination_id"], record["context"]
    )
    name = record["name"]
    index = 2
    while True:
        try:
            if record["backend"] == "codex":
                await target._client.set_thread_name(record["destination_id"], name)
            else:
                await target._client.rename_by_label(
                    LabelQueryInput(thread_id=record["destination_id"]), name
                )
            break
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" not in str(exc) or index > 100:
                raise
            # Claude labels are unique. Retry only the rename of our own
            # already-created thread, never creation or another thread.
            name = f"{record['base_name']} ({index}) · {BACKENDS[record['backend']]}"
            index += 1
    set_thread_origin(record["destination_id"], MANUAL_ORIGIN)
    record = store.update(record["operation_id"], state="ready", name=name)
    return response(record)


def response(record: dict) -> dict:
    return {
        "thread_id": record["destination_id"],
        "backend": record["backend"],
        "name": record["name"],
        "continued_from": {
            "thread_id": record["source_id"],
            "name": record["source_name"],
        },
    }
