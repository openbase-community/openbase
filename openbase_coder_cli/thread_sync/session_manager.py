"""Openbase thread manager backed by the Super Agents Codex client."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any, Callable

from super_agents.app_models import LabelQueryInput
from super_agents.app_server_client import (
    extract_notification_thread_id,
    extract_notification_turn_id,
    extract_turn_id,
)

from openbase_coder_cli.backend_config import (
    configured_execution_backend as _configured_execution_backend,
)
from openbase_coder_cli.livekit_voice_history import record_voice_assignment
from openbase_coder_cli.livekit_voice_route import (
    super_agent_voice_for_context,
)

from .models import ThreadStatus as SessionStatus
from .models import TurnInfo as RunInfo
from .session_manager_approvals import SessionManagerApprovalsMixin
from .session_manager_base import (
    ThreadListPage,
    _configured_model_for_role,
    _default_client_for_execution_backend,
    _has_livekit_voice_route,
    _is_thread_unavailable_error,
    _RoutineClient,
    _SuperAgentsClient,
    _with_dispatcher_onboarding_reminder,
    load_super_agent_developer_instructions,
    logger,
    resolve_super_agent_instructions_path,
)
from .session_manager_routines import SessionManagerRoutinesMixin
from .session_manager_threads import SessionManagerThreadsMixin
from .session_manager_turns import SessionManagerTurnsMixin
from .thread_payloads import (
    _optional_turn_string,
    _timestamp_to_datetime,
    _undelivered_suffix,
)

# The shared helpers, protocols and app-server client subclass moved to
# ``session_manager_base`` and the method clusters moved to the mixin modules;
# keep the names other modules and tests import from here re-exported.
__all__ = [
    "CodexAppServerSessionManager",
    "ThreadListPage",
    "_RoutineClient",
    "_SuperAgentsClient",
    "_configured_execution_backend",
    "_default_client_for_execution_backend",
    "get_session_manager",
    "load_super_agent_developer_instructions",
    "resolve_super_agent_instructions_path",
]


async def _broadcast(session_id: str, event: dict[str, Any]) -> None:
    """Broadcast an event to the WebSocket group for a thread."""
    try:
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        group_name = f"thread_{session_id}"
        await channel_layer.group_send(group_name, event)

        if event.get("type") in ("turn_started", "turn_completed", "error"):
            global_event = {**event, "thread_id": session_id}
            await channel_layer.group_send("all_threads", global_event)
    except Exception:
        logger.warning(
            "Failed to broadcast %s event for thread %s",
            event.get("type"),
            session_id,
            exc_info=True,
        )


def _turn_failure_message(params: dict[str, Any]) -> str:
    error = params.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    if isinstance(error, str) and error.strip():
        return error.strip()
    return "The agent turn failed unexpectedly."


def _notification_item_id(params: dict[str, Any]) -> str | None:
    for key in ("itemId", "item_id"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    item = params.get("item")
    if isinstance(item, dict):
        value = item.get("id")
        if isinstance(value, str) and value:
            return value
    return None


def _agent_message_boundary(previous_text: str, next_text: str) -> str:
    if not previous_text or not next_text:
        return ""
    if previous_text[-1].isspace() or next_text[0].isspace():
        return ""
    return "\n\n"


class CodexAppServerSessionManager(
    SessionManagerThreadsMixin,
    SessionManagerTurnsMixin,
    SessionManagerRoutinesMixin,
    SessionManagerApprovalsMixin,
):
    """Openbase-compatible thread facade backed by Super Agents."""

    def __init__(
        self,
        ws_url: str | None = None,
        client: _SuperAgentsClient | None = None,
        routine_client: _RoutineClient | None = None,
        model_for_role: Callable[[str], str | None] | None = None,
    ) -> None:
        self._ws_url = ws_url or os.environ.get(
            "CODEX_APP_SERVER_URL", "ws://127.0.0.1:4500"
        )
        self._uses_external_client = client is not None
        self._execution_backend = _configured_execution_backend()
        self._client: _SuperAgentsClient = client or self._default_client(
            self._execution_backend
        )
        self._routine_client: _RoutineClient | None = routine_client
        self._model_for_role = model_for_role or _configured_model_for_role
        self._turn_to_session: dict[str, str] = {}
        self._delivered_text: dict[str, str] = {}
        self._turn_current_item: dict[str, str] = {}
        self._delivered_item_text: dict[tuple[str, str], str] = {}
        self._turn_prompt: dict[str, str] = {}
        self._turn_steers: dict[str, list[Any]] = {}
        self._state_lock = asyncio.Lock()

    def _default_client(self, execution_backend: str) -> _SuperAgentsClient:
        return _default_client_for_execution_backend(
            manager=self,
            ws_url=self._ws_url,
            execution_backend=execution_backend,
        )

    async def send_message(self, session_id: str, message: str) -> str:
        """Start a turn on a Codex app-server thread."""
        thread = await self.get_session_state(session_id)
        if thread is None:
            raise ValueError(f"Thread {session_id} not found")
        if thread.current_run is not None and thread.current_run.status in {
            SessionStatus.running,
            SessionStatus.waiting,
        }:
            raise ValueError(
                f"Thread {session_id} already has an active turn. Interrupt it first."
            )
        if not thread.directory:
            raise ValueError(f"Thread {session_id} is missing its cwd")

        message = _with_dispatcher_onboarding_reminder(session_id, message)

        model = self._model_for_thread(thread)
        role_turn_input = {
            "prompt": message,
            "cwd": thread.directory,
            **self._codex_permission_defaults(),
        }
        if model:
            role_turn_input["model"] = model

        if self._uses_backend_session_api():
            started = await self._client.start_turn_by_label(
                LabelQueryInput(thread_id=session_id, cwd=thread.directory),
                role_turn_input,
            )
            turn_id = extract_turn_id(started)
            if not turn_id:
                raise RuntimeError("Super Agents did not return a turn id")
            async with self._state_lock:
                self._remember_turn_prompt_locked(turn_id, message)
            return turn_id

        turn_input = {
            "threadId": session_id,
            **role_turn_input,
        }
        try:
            started = await self._client.start_turn(turn_input)
        except RuntimeError as exc:
            if not _is_thread_unavailable_error(exc):
                raise
            await self._resume_thread(session_id, thread.directory)
            started = await self._client.start_turn(turn_input)
        turn_id = extract_turn_id(started)
        if not turn_id:
            raise RuntimeError("Super Agents did not return a turn id")
        agent_name = thread.agent_name
        voice = super_agent_voice_for_context(session_id, thread.name, agent_name)
        logger.info(
            "livekit_voice_assignment_super_agent_turn thread_id=%s thread_name=%s "
            "agent_name=%s voice_id=%s voice_name=%s route_active=%s",
            session_id,
            thread.name or "",
            agent_name or "",
            voice.voice_id if voice else "",
            voice.name if voice else "",
            _has_livekit_voice_route(),
        )
        if agent_name and voice is not None and _has_livekit_voice_route():
            record_voice_assignment(
                thread_id=session_id,
                agent_name=agent_name,
                cwd=thread.directory,
                voice_id=voice.voice_id,
                voice_name=voice.name,
                kind="codex_thread",
                source="super_agent_start",
            )
        run = RunInfo(
            run_id=turn_id,
            started_at=datetime.now(UTC),
            status=SessionStatus.running,
            message=message,
            reasoning_effort=_optional_turn_string(
                started,
                "reasoningEffort",
                "reasoning_effort",
            ),
        )
        async with self._state_lock:
            self._turn_to_session[turn_id] = session_id
            self._delivered_text[turn_id] = ""
            self._remember_turn_prompt_locked(turn_id, message)

        await _broadcast(
            session_id,
            {"type": "turn_started", "data": run.model_dump(mode="json")},
        )
        return turn_id

    async def _broadcast_thread_state(self, thread_id: str) -> None:
        session_state = await self.get_session_state(thread_id)
        if session_state is not None:
            await _broadcast(
                thread_id,
                {
                    "type": "thread_state",
                    "data": session_state.model_dump(mode="json"),
                },
            )

    def handle_client_event(self, method: str, params: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._handle_client_event(method, params))

    async def _handle_client_event(self, method: str, params: dict[str, Any]) -> None:
        thread_id = extract_notification_thread_id(params)
        turn_id = extract_notification_turn_id(params)
        if turn_id and not thread_id:
            async with self._state_lock:
                thread_id = self._turn_to_session.get(turn_id)
        if not thread_id:
            return

        if method == "server_request":
            await self._broadcast_thread_state(thread_id)
            return

        if method == "turn/started":
            if turn_id:
                await self._announce_started_turn(thread_id, turn_id, params)
            return

        if method == "item/agentMessage/delta":
            delta = params.get("delta", "")
            if turn_id and isinstance(delta, str) and delta:
                await self._append_output(
                    thread_id,
                    turn_id,
                    delta,
                    item_id=_notification_item_id(params),
                )
            return

        if method == "item/completed":
            item = params.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = item.get("text", "")
                if turn_id and isinstance(text, str) and text:
                    item_id = _notification_item_id(params)
                    if item_id:
                        delivered = self._delivered_item_text.get(
                            (turn_id, item_id),
                            "",
                        )
                    else:
                        delivered = self._delivered_text.get(turn_id, "")
                    suffix = _undelivered_suffix(delivered, text)
                    if suffix:
                        await self._append_output(
                            thread_id,
                            turn_id,
                            suffix,
                            item_id=item_id,
                        )
            return

        if method in {"turn/completed", "turn/failed"}:
            if turn_id:
                async with self._state_lock:
                    self._forget_turn_locked(turn_id)
            if method == "turn/failed":
                failure_message = _turn_failure_message(params)
                logger.error(
                    "Codex turn %s failed for thread %s: %s",
                    turn_id or "",
                    thread_id,
                    failure_message,
                )
                await _broadcast(
                    thread_id,
                    {
                        "type": "error",
                        "data": {
                            "message": failure_message,
                            "code": "turn_failed",
                            "turn_id": turn_id or "",
                        },
                    },
                )
            session_state = await self.get_session_state(thread_id)
            if session_state is not None:
                await _broadcast(
                    thread_id,
                    {
                        "type": "turn_completed",
                        "data": session_state.model_dump(mode="json"),
                    },
                )

    async def _announce_started_turn(
        self,
        thread_id: str,
        turn_id: str,
        params: dict[str, Any],
    ) -> None:
        """Broadcast turn_started for turns this process did not start itself.

        Queued turns are dequeued and started inside the Super Agents client,
        so the turn/started notification is the only signal that a new turn
        (with a new prompt) replaced the previous one.
        """
        async with self._state_lock:
            already_known = turn_id in self._turn_to_session
            self._turn_to_session[turn_id] = thread_id
            self._delivered_text.setdefault(turn_id, "")
        if already_known:
            return

        session_state = await self.get_session_state(thread_id)
        run: RunInfo | None = None
        if (
            session_state is not None
            and session_state.current_run is not None
            and session_state.current_run.run_id == turn_id
        ):
            run = session_state.current_run
        if run is None:
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            started_at_raw = turn.get("startedAt")
            async with self._state_lock:
                tracked_prompt = self._turn_prompt.get(turn_id, "")
            run = RunInfo(
                run_id=turn_id,
                started_at=(
                    _timestamp_to_datetime(started_at_raw)
                    if started_at_raw
                    else datetime.now(UTC)
                ),
                status=SessionStatus.running,
                message=tracked_prompt,
                reasoning_effort=_optional_turn_string(
                    turn, "reasoningEffort", "reasoning_effort"
                ),
            )
        await _broadcast(
            thread_id,
            {"type": "turn_started", "data": run.model_dump(mode="json")},
        )
        # Follow with full state (updated queue, history) only when the read
        # already reflects the new turn; a lagging read would clobber the
        # freshly announced current turn on clients.
        if (
            session_state is not None
            and session_state.current_run is not None
            and session_state.current_run.run_id == turn_id
        ):
            await _broadcast(
                thread_id,
                {
                    "type": "thread_state",
                    "data": session_state.model_dump(mode="json"),
                },
            )

    async def _append_output(
        self,
        thread_id: str,
        turn_id: str,
        text: str,
        *,
        item_id: str | None = None,
    ) -> None:
        async with self._state_lock:
            previous_text = self._delivered_text.get(turn_id, "")
            output_text = text
            if item_id:
                previous_item_id = self._turn_current_item.get(turn_id)
                if previous_item_id is not None and previous_item_id != item_id:
                    output_text = _agent_message_boundary(previous_text, text) + text
                self._turn_current_item[turn_id] = item_id
                item_key = (turn_id, item_id)
                self._delivered_item_text[item_key] = (
                    self._delivered_item_text.get(item_key, "") + text
                )
            self._delivered_text[turn_id] = previous_text + output_text
        await _broadcast(
            thread_id,
            {
                "type": "output_update",
                "data": {
                    "stream": "stdout",
                    "line": output_text,
                    "chunk": True,
                    "turn_id": turn_id,
                },
            },
        )


_session_manager: CodexAppServerSessionManager | None = None


def get_session_manager() -> CodexAppServerSessionManager:
    """Get the singleton thread manager instance."""
    global _session_manager
    execution_backend = _configured_execution_backend()
    if _session_manager is None or (
        not _session_manager._uses_external_client
        and _session_manager._execution_backend != execution_backend
    ):
        _session_manager = CodexAppServerSessionManager()
    return _session_manager
