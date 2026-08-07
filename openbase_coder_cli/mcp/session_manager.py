"""Openbase thread manager backed by the Super Agents Codex client."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from super_agents.app_models import LabelQueryInput
from super_agents.app_permissions import permission_response_for_request
from super_agents.app_server_client import (
    CodexAppServerClient,
    extract_notification_thread_id,
    extract_notification_turn_id,
    extract_threads,
    extract_turn_id,
    find_latest_turn,
    login_shell_config_override,
    shared_permission_requests,
    write_shared_permission_decision,
)
from super_agents.backend_clients import (
    CLAUDE_CODE_BACKEND,
    backend_from_environment,
)

from openbase_coder_cli.backend_config import (
    CODEX_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)
from openbase_coder_cli.cli.backend import read_backend
from openbase_coder_cli.codex_session_defaults import codex_permission_defaults
from openbase_coder_cli.dispatcher_config import (
    DISPATCHER_MODEL_ROLE,
    SUPER_AGENTS_MODEL_ROLE,
    dispatcher_model,
    super_agents_model,
)
from openbase_coder_cli.livekit_voice_history import record_voice_assignment
from openbase_coder_cli.livekit_voice_route import (
    get_livekit_voice_route_state,
    super_agent_voice_for_context,
)
from openbase_coder_cli.onboarding_reminder import append_onboarding_reminder
from openbase_coder_cli.paths import (
    CODEX_SUPER_AGENT_INSTRUCTIONS_PATH,
    DEFAULT_ENV_FILE_PATH,
)

from .models import QueuedTurnInfo
from .models import ThreadInfo as SessionInfo
from .models import ThreadStatus as SessionStatus
from .models import TurnInfo as RunInfo
from .models import TurnSteerInfo as SteerInfo
from .thread_payloads import (
    _approval_request_payload,
    _datetime_to_iso,
    _merge_tracked_turn_details,
    _merge_tracked_turn_summaries,
    _next_cursor,
    _normalize_backend_thread_payload,
    _optional_turn_string,
    _session_from_thread,
    _session_sort_key,
    _thread_history_limit,
    _thread_payload,
    _timestamp_to_datetime,
    _undelivered_suffix,
)

logger = logging.getLogger(__name__)

SUPER_AGENT_INSTRUCTIONS_PATH_ENV = "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH"
# Locally tracked per-turn prompts/steers are kept after turn completion (see
# _forget_turn_locked) and bounded by evicting the oldest entries.
_TRACKED_TURN_TEXT_LIMIT = 500
SUPER_AGENT_INSTRUCTIONS_TEXT_ENV = "CODEX_SUPER_AGENT_INSTRUCTIONS"
_USE_SUPER_AGENT_INSTRUCTIONS = object()


@dataclass(frozen=True)
class ThreadListPage:
    threads: list[SessionInfo]
    next_cursor: str | None


class _SuperAgentsClient(Protocol):
    async def ensure_connected(self) -> None: ...
    async def list_threads(
        self,
        use_state_db_only: bool = True,
        search_term: str | None = None,
        cwd: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...
    async def read_thread(
        self,
        thread_id: str,
        include_turns: bool = True,
    ) -> dict[str, Any]: ...
    async def start_thread(self, input_data: dict[str, Any]) -> dict[str, Any]: ...
    async def start_turn(self, input_data: dict[str, Any]) -> dict[str, Any]: ...
    async def start_turn_by_label(
        self,
        input_data: LabelQueryInput,
        turn_input: dict[str, Any],
    ) -> dict[str, Any]: ...
    async def queue_turn_by_label(
        self,
        input_data: LabelQueryInput,
        turn_input: dict[str, Any],
    ) -> dict[str, Any]: ...
    async def steer_by_label(
        self,
        input_data: LabelQueryInput,
        prompt: str,
        turn_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    async def cancel_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]: ...
    def pending_permission_requests(self) -> list[Any]: ...
    async def answer_request(
        self,
        request_id: str | int,
        result: dict[str, Any],
    ) -> dict[str, Any]: ...
    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: float = 30,
    ) -> dict[str, Any]: ...
    async def merge_session(
        self,
        thread_id: str,
        patch: dict[str, Any],
        *,
        clear_fields: list[str] | None = None,
    ) -> None: ...
    async def get_session(self, thread_id: str) -> Any: ...


class _RoutineClient(Protocol):
    async def save_routine(self, input_data: dict[str, Any]) -> dict[str, Any]: ...
    async def list_routines(self) -> dict[str, Any]: ...
    async def read_routine(self, name: str) -> dict[str, Any]: ...
    async def delete_routine(self, name: str) -> dict[str, Any]: ...
    async def run_due_routines(
        self,
        name: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]: ...


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


def _read_instruction_file(path: Path) -> str | None:
    try:
        loaded = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning(
            "Unable to read Super Agent instruction file %s",
            path,
            exc_info=True,
        )
        return None
    return loaded or None


def resolve_super_agent_instructions_path(
    *,
    env: dict[str, str] | None = None,
    default_path: Path | None = None,
) -> Path:
    values = env if env is not None else os.environ
    explicit_path = values.get(SUPER_AGENT_INSTRUCTIONS_PATH_ENV, "").strip()
    if explicit_path:
        return Path(explicit_path).expanduser()
    return default_path or CODEX_SUPER_AGENT_INSTRUCTIONS_PATH


def load_super_agent_developer_instructions(
    *,
    env: dict[str, str] | None = None,
    default_path: Path | None = None,
) -> str | None:
    values = env if env is not None else os.environ
    loaded = _read_instruction_file(
        resolve_super_agent_instructions_path(env=values, default_path=default_path)
    )
    if loaded:
        return loaded

    text = values.get(SUPER_AGENT_INSTRUCTIONS_TEXT_ENV, "").strip()
    return text or None


def _is_payload_too_large_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "message too big" in message
        or "exceeds limit" in message
        or "sent 1009" in message
        or "received 1009" in message
    )


def _runtime_error_message(exc: RuntimeError) -> str:
    raw = str(exc)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"]
    return raw


def _is_thread_unavailable_error(exc: RuntimeError) -> bool:
    message = _runtime_error_message(exc).lower()
    return "not found" in message or "invalid thread id" in message


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


class _OpenbaseSuperAgentsClient(CodexAppServerClient):
    def __init__(
        self, manager: "CodexAppServerSessionManager", ws_url: str | None
    ) -> None:
        super().__init__(ws_url=ws_url)
        self._manager = manager

    async def start_managed_server(self) -> None:
        """Openbase owns the Codex app-server lifecycle through launchd services."""
        raise RuntimeError(
            f"Codex app-server is not ready at {self.ws_url}; "
            "start the Openbase codex-app-server service instead."
        )

    def handle_server_request(
        self,
        request_id: str | int,
        method: str,
        params: dict[str, Any],
    ) -> None:
        super().handle_server_request(request_id, method, params)
        self._manager.handle_client_event("server_request", params)

    def handle_notification(self, method: str, params: dict[str, Any]) -> None:
        super().handle_notification(method, params)
        self._manager.handle_client_event(method, params)


class CodexAppServerSessionManager:
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
        self._turn_steers: dict[str, list[SteerInfo]] = {}
        self._state_lock = asyncio.Lock()

    def _default_client(self, execution_backend: str) -> _SuperAgentsClient:
        return _default_client_for_execution_backend(
            manager=self,
            ws_url=self._ws_url,
            execution_backend=execution_backend,
        )

    def _routines_client(self) -> _RoutineClient:
        if self._routine_client is not None:
            return self._routine_client
        if _supports_routine_methods(self._client):
            self._routine_client = self._client
        else:
            self._routine_client = _OpenbaseSuperAgentsClient(self, self._ws_url)
        return self._routine_client

    def _uses_backend_session_api(self) -> bool:
        return not callable(getattr(self._client, "read_thread", None))

    def _codex_permission_defaults(self) -> dict[str, str]:
        if self._uses_backend_session_api():
            return {}
        return codex_permission_defaults()

    def _model_for_thread(self, thread: SessionInfo) -> str | None:
        role = (
            DISPATCHER_MODEL_ROLE
            if thread.name and thread.name.casefold() == "dispatcher"
            else SUPER_AGENTS_MODEL_ROLE
        )
        return self._model_for_role(role)

    async def create_thread(
        self,
        directory: str,
        thread_id: str | None = None,
    ) -> SessionInfo:
        """Create a fresh Codex app-server thread for the directory."""
        return await self.create_session(
            directory,
            session_id=thread_id,
            reuse_existing=thread_id is not None,
        )

    async def archive_thread(self, thread_id: str) -> bool:
        """Archive a Codex app-server thread."""
        return await self.close_session(thread_id)

    async def start_turn(self, thread_id: str, prompt: str) -> str:
        """Start a new Codex turn on an existing thread."""
        return await self.send_message(thread_id, prompt)

    async def queue_turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        """Queue a follow-up turn after the active turn, or start immediately if idle."""
        thread = await self.get_session_state(thread_id)
        if thread is None:
            raise ValueError(f"Thread {thread_id} not found")
        if not thread.directory:
            raise ValueError(f"Thread {thread_id} is missing its cwd")

        prompt = _with_dispatcher_onboarding_reminder(thread_id, prompt)

        turn_input = {
            "prompt": prompt,
            "cwd": thread.directory,
            **self._codex_permission_defaults(),
        }
        if model := self._model_for_thread(thread):
            turn_input["model"] = model

        result = await self._client.queue_turn_by_label(
            LabelQueryInput(thread_id=thread_id, cwd=thread.directory),
            turn_input,
        )
        if not result.get("queued"):
            turn_id = extract_turn_id(result)
            if turn_id:
                async with self._state_lock:
                    self._turn_to_session[turn_id] = thread_id
                    self._delivered_text[turn_id] = ""
                    self._remember_turn_prompt_locked(turn_id, prompt)
        await self._broadcast_thread_state(thread_id)
        return result

    async def steer_turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        """Send steering input to the active turn on a thread."""
        thread = await self.get_session_state(thread_id)
        if thread is None:
            raise ValueError(f"Thread {thread_id} not found")
        if not thread.directory:
            raise ValueError(f"Thread {thread_id} is missing its cwd")

        turn_id = await self._active_turn_id(thread_id)
        if turn_id is None:
            raise ValueError(f"Thread {thread_id} has no active turn to steer")

        result = await self._client.steer_by_label(
            LabelQueryInput(
                thread_id=thread_id,
                cwd=thread.directory,
                turn_id=turn_id,
                prefer="latest_active",
            ),
            prompt,
            {"cwd": thread.directory},
        )
        resolved_turn_id = extract_turn_id(result) or turn_id
        # steer_by_label can fall back to starting or queueing a fresh turn
        # when the resolved turn is no longer steerable.
        steered = not result.get("queued") and not result.get("startedImmediately")
        async with self._state_lock:
            self._turn_to_session[resolved_turn_id] = thread_id
            self._delivered_text.setdefault(resolved_turn_id, "")
            if steered:
                self._remember_turn_steer_locked(
                    resolved_turn_id,
                    SteerInfo(text=prompt, created_at=datetime.now(UTC)),
                )
            else:
                self._remember_turn_prompt_locked(resolved_turn_id, prompt)
        await self._broadcast_thread_state(thread_id)
        return {**result, "turn_id": resolved_turn_id, "steered": steered}

    async def get_thread_state(self, thread_id: str) -> SessionInfo | None:
        """Get the current thread snapshot."""
        return await self.get_session_state(thread_id)

    async def interrupt_turn(self, thread_id: str) -> bool:
        """Interrupt the current turn on a thread."""
        return await self.interrupt_run(thread_id)

    async def list_approval_requests(self) -> list[dict[str, Any]]:
        """List currently pending app-server approval requests across threads."""
        requests_by_id = {
            str(request.get("id")): request
            for request in shared_permission_requests()
            if request.get("id") is not None
        }
        try:
            await self._client.ensure_connected()
            for request in self._client.pending_permission_requests():
                payload = _approval_request_payload(request)
                if payload.get("id") is not None:
                    requests_by_id[str(payload["id"])] = payload
        except Exception:
            logger.debug("Unable to merge in-process approval requests", exc_info=True)
        return [
            _approval_request_payload(request) for request in requests_by_id.values()
        ]

    async def answer_approval_request(
        self,
        request_id: str | int,
        decision: Literal["accept", "decline", "cancel"],
    ) -> dict[str, Any]:
        """Answer one pending app-server approval request."""
        await self._client.ensure_connected()
        request = self._find_pending_approval_request(request_id)
        if request is None:
            shared_request = _find_shared_permission_request(request_id)
            if write_shared_permission_decision(request_id, decision):
                return {
                    "answered": False,
                    "queued": True,
                    "requestId": request_id,
                    "result": permission_response_for_request(
                        shared_request or {"method": ""},
                        decision,
                    ),
                }
            raise ValueError(f"No pending approval request found for id {request_id}.")
        return await self._client.answer_request(
            request.id,
            permission_response_for_request(request, decision),
        )

    def _find_pending_approval_request(self, request_id: str | int) -> Any | None:
        candidates: list[str | int] = [request_id]
        if isinstance(request_id, str) and request_id.isdigit():
            candidates.append(int(request_id))
        candidate_strings = {str(item) for item in candidates}
        for request in self._client.pending_permission_requests():
            if request.id in candidates or str(request.id) in candidate_strings:
                return request
        return None

    async def list_routines(self) -> dict[str, Any]:
        """List persisted Super Agents routines."""
        return await self._routines_client().list_routines()

    async def read_routine(self, name: str) -> dict[str, Any]:
        """Read one persisted Super Agents routine."""
        return await self._routines_client().read_routine(name)

    async def save_routine(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Create or update a persisted Super Agents routine."""
        return await self._routines_client().save_routine(input_data)

    async def delete_routine(self, name: str) -> dict[str, Any]:
        """Delete one persisted Super Agents routine."""
        return await self._routines_client().delete_routine(name)

    async def run_due_routines(
        self,
        name: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Run due routines through the Super Agents client library."""
        return await self._routines_client().run_due_routines(name=name, force=force)

    async def resume_thread_with_developer_instructions(
        self,
        thread_id: str,
        directory: str,
        developer_instructions: str,
    ) -> None:
        """Resume a thread with explicit developer instructions."""
        await self._resume_thread(
            thread_id,
            directory,
            developer_instructions=developer_instructions,
        )

    async def resume_thread_without_developer_instructions(
        self,
        thread_id: str,
        directory: str,
    ) -> None:
        """Resume a thread without changing its developer instructions."""
        await self._resume_thread(
            thread_id,
            directory,
            developer_instructions=None,
        )

    async def _resume_thread(
        self,
        thread_id: str,
        directory: str,
        *,
        developer_instructions: str | None | object = _USE_SUPER_AGENT_INSTRUCTIONS,
    ) -> None:
        if developer_instructions is _USE_SUPER_AGENT_INSTRUCTIONS:
            effective_developer_instructions = load_super_agent_developer_instructions()
        elif isinstance(developer_instructions, str):
            effective_developer_instructions = developer_instructions
        else:
            effective_developer_instructions = None

        if self._uses_backend_session_api():
            if effective_developer_instructions is not None:
                resume_by_label = getattr(self._client, "resume_by_label", None)
                if callable(resume_by_label):
                    await resume_by_label(
                        LabelQueryInput(thread_id=thread_id, cwd=directory),
                        developer_instructions=effective_developer_instructions,
                    )
                    return
            read_by_label = getattr(self._client, "read_by_label", None)
            if callable(read_by_label):
                await read_by_label(
                    LabelQueryInput(thread_id=thread_id, cwd=directory),
                    include_turns=False,
                )
            return

        await self._client.ensure_connected()
        params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": directory,
            **self._codex_permission_defaults(),
            "config": await login_shell_config_override(),
        }
        if effective_developer_instructions is not None:
            params["developerInstructions"] = effective_developer_instructions
        await self._client.request("thread/resume", params)
        await self._client.merge_session(
            thread_id,
            {
                "threadId": thread_id,
                "cwd": directory,
                "lastStatus": "unknown",
                "updatedAt": _datetime_to_iso(datetime.now(UTC)),
            },
        )

    async def list_threads(self) -> list[SessionInfo]:
        """List stored Codex threads through Super Agents."""
        return await self.list_sessions()

    # The Codex app-server returns thread/list pages in thread-creation
    # order, so recency ranking has to fetch the whole recent window and
    # re-sort by update time; sorting single pages would leave an
    # old-but-recently-active thread buried at its creation position.
    RECENCY_WINDOW_THREADS = 500
    RECENCY_FETCH_PAGE_SIZE = 100

    async def list_thread_page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> ThreadListPage:
        """List one stored Codex thread page through Super Agents."""
        if self._uses_backend_session_api():
            return await self._backend_thread_page(limit=limit, cursor=cursor)

        sessions = await self._recency_ranked_threads()
        try:
            start = int(cursor or 0)
        except ValueError:
            start = 0
        end = start + limit
        return ThreadListPage(
            threads=sessions[start:end],
            next_cursor=str(end) if end < len(sessions) else None,
        )

    async def _recency_ranked_threads(self) -> list[SessionInfo]:
        raw_threads: list[dict[str, Any]] = []
        fetch_cursor: str | None = None
        while len(raw_threads) < self.RECENCY_WINDOW_THREADS:
            result = await self._list_thread_page_result(
                limit=self.RECENCY_FETCH_PAGE_SIZE,
                cursor=fetch_cursor,
            )
            page_threads = extract_threads(result)
            if not page_threads:
                break
            raw_threads.extend(page_threads)
            fetch_cursor = _next_cursor(result)
            if not fetch_cursor:
                break
        sessions = [
            _session_from_thread(thread, include_turns=False) for thread in raw_threads
        ]
        return sorted(sessions, key=_session_sort_key, reverse=True)

    async def _backend_thread_page(
        self,
        *,
        limit: int,
        cursor: str | None,
    ) -> ThreadListPage:
        sessions = await self._backend_sessions()
        sorted_sessions = sorted(sessions, key=_session_sort_key, reverse=True)
        try:
            start = int(cursor or 0)
        except ValueError:
            start = 0
        end = start + limit
        return ThreadListPage(
            threads=sorted_sessions[start:end],
            next_cursor=str(end) if end < len(sorted_sessions) else None,
        )

    async def _backend_sessions(self) -> list[SessionInfo]:
        sessions_method = getattr(self._client, "sessions", None)
        if not callable(sessions_method):
            return []
        raw_sessions = await sessions_method()
        return [
            _session_from_thread(
                _normalize_backend_thread_payload(session),
                include_turns=False,
            )
            for session in raw_sessions
            if isinstance(session, dict)
        ]

    async def _list_thread_page_result(
        self,
        *,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        if self._uses_external_client:
            if cursor:
                await self._client.ensure_connected()
                return await self._client.request(
                    "thread/list",
                    {
                        "useStateDbOnly": True,
                        "limit": limit,
                        "cursor": cursor,
                        "modelProviders": [],
                    },
                )
            return await self._client.list_threads(
                True,
                limit=limit,
            )

        client = _OpenbaseSuperAgentsClient(self, self._ws_url)
        try:
            await client.ensure_connected()
            # The empty modelProviders list disables the app server's
            # active-provider filter: switching coding backends must not
            # hide threads created under the other provider.
            params: dict[str, Any] = {
                "useStateDbOnly": True,
                "limit": limit,
                "modelProviders": [],
            }
            if cursor:
                params["cursor"] = cursor
            return await client.request("thread/list", params)
        finally:
            await client.close()

    async def create_session(
        self,
        directory: str,
        session_id: str | None = None,
        session_type: Literal["codex"] = "codex",
        reuse_existing: bool = True,
    ) -> SessionInfo:
        """Create or reuse a Codex app-server thread for the directory."""
        if session_type != "codex":
            raise ValueError("session_type must be 'codex'")

        expanded_dir = str(Path(directory).expanduser().resolve())
        if not os.path.isdir(expanded_dir):
            raise ValueError(f"Directory does not exist: {expanded_dir}")

        if session_id is not None:
            session = await self.get_session_state(session_id)
            if session is None:
                raise ValueError(f"Thread {session_id} not found")
            return session

        if self._uses_backend_session_api():
            existing_sessions = await self._backend_sessions()
            if reuse_existing:
                for session in existing_sessions:
                    if session.directory == expanded_dir:
                        return session
            name = Path(expanded_dir).name or f"thread-{uuid.uuid4().hex[:8]}"
            if reuse_existing and any(
                session.name == name for session in existing_sessions
            ):
                name = f"{name}-{uuid.uuid4().hex[:8]}"
            thread_input = {
                "name": name,
                "cwd": expanded_dir,
                **self._codex_permission_defaults(),
            }
            if not reuse_existing:
                thread_input["fresh"] = True
            if model := self._model_for_role(SUPER_AGENTS_MODEL_ROLE):
                thread_input["model"] = model
            developer_instructions = load_super_agent_developer_instructions()
            if developer_instructions is not None:
                thread_input["developerInstructions"] = developer_instructions
            started = await self._client.start_thread(thread_input)
            return _session_from_thread(
                _normalize_backend_thread_payload(started),
                include_turns=False,
            )

        if reuse_existing:
            result = await self._client.list_threads(
                True,
                cwd=expanded_dir,
                limit=1,
            )
            existing = extract_threads(result)
            if existing:
                return _session_from_thread(existing[0], include_turns=False)

        thread_input = {"cwd": expanded_dir, **self._codex_permission_defaults()}
        if model := self._model_for_role(SUPER_AGENTS_MODEL_ROLE):
            thread_input["model"] = model
        developer_instructions = load_super_agent_developer_instructions()
        if developer_instructions is not None:
            thread_input["developerInstructions"] = developer_instructions

        started = await self._client.start_thread(thread_input)
        thread = _thread_payload(started)
        if thread is None:
            raise RuntimeError("Super Agents did not return a thread")
        return _session_from_thread(thread, include_turns=False)

    async def close_session(self, session_id: str) -> bool:
        """Archive a persisted thread."""
        await self.interrupt_run(session_id)
        if self._uses_backend_session_api():
            return await self.get_session_state(session_id) is not None
        try:
            await self._client.ensure_connected()
            await self._client.request("thread/archive", {"threadId": session_id})
        except RuntimeError as exc:
            if _is_thread_unavailable_error(exc):
                return False
            raise
        async with self._state_lock:
            turn_ids = [
                turn_id
                for turn_id, candidate_session_id in self._turn_to_session.items()
                if candidate_session_id == session_id
            ]
            for turn_id in turn_ids:
                self._forget_turn_locked(turn_id)
        return True

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

    async def get_session_state(self, session_id: str) -> SessionInfo | None:
        """Get the current thread snapshot."""
        result = await self._read_thread(session_id, include_turns=True)
        if result is None:
            return None
        session = _session_from_thread(result, include_turns=True)
        await self._apply_local_turn_state(session_id, session)
        return session

    async def _apply_local_turn_state(
        self, session_id: str, session: SessionInfo
    ) -> None:
        """Overlay locally tracked prompts, steers, and the pending turn queue.

        The app-server can lag behind locally initiated actions — a turn's
        userMessage items (including steering input) may not be readable while
        the turn is in flight — so locally tracked state fills those gaps.
        """
        async with self._state_lock:
            for run in [session.current_run, *session.run_history]:
                if run is None:
                    continue
                if not run.message:
                    run.message = self._turn_prompt.get(run.run_id, "")
                tracked_steers = self._turn_steers.get(run.run_id)
                if tracked_steers:
                    known = {steer.text.strip() for steer in run.steers}
                    run.steers = run.steers + [
                        steer
                        for steer in tracked_steers
                        if steer.text.strip() not in known
                    ]
        session.queued_turns = self._queued_turns_for_thread(session_id)

    def _queued_turns_for_thread(self, thread_id: str) -> list[QueuedTurnInfo]:
        summary_method = getattr(self._client, "queued_turn_summary", None)
        if not callable(summary_method):
            return []
        try:
            summaries = summary_method()
        except Exception:
            logger.debug(
                "Unable to read queued turns for thread %s", thread_id, exc_info=True
            )
            return []
        queued: list[QueuedTurnInfo] = []
        for summary in summaries:
            if not isinstance(summary, dict) or summary.get("threadId") != thread_id:
                continue
            for item in summary.get("items", []):
                if not isinstance(item, dict):
                    continue
                input_data = (
                    item.get("inputData")
                    if isinstance(item.get("inputData"), dict)
                    else {}
                )
                prompt = str(
                    input_data.get("prompt") or item.get("promptPreview") or ""
                )
                if not prompt:
                    continue
                queued_at_raw = item.get("queuedAt")
                queued.append(
                    QueuedTurnInfo(
                        queue_id=str(item.get("id")) if item.get("id") else None,
                        prompt=prompt,
                        queued_at=(
                            _timestamp_to_datetime(queued_at_raw)
                            if queued_at_raw
                            else None
                        ),
                    )
                )
        return queued

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

    async def interrupt_run(self, session_id: str) -> bool:
        """Interrupt the current turn in a thread."""
        if self._uses_backend_session_api():
            try:
                result = await self._client.cancel_by_label(
                    LabelQueryInput(thread_id=session_id)
                )
            except RuntimeError as exc:
                message = _runtime_error_message(exc).lower()
                if _is_thread_unavailable_error(exc) or "no active" in message:
                    return False
                raise
            return bool(result.get("cancelled", True))

        turn_id = await self._active_turn_id(session_id)
        if turn_id is None:
            return False
        try:
            await self._client.cancel_turn(session_id, turn_id)
        except RuntimeError as exc:
            message = _runtime_error_message(exc).lower()
            if _is_thread_unavailable_error(exc) or "no active" in message:
                return False
            raise
        return True

    async def list_sessions(self) -> list[SessionInfo]:
        """List stored Codex threads through Super Agents."""
        if self._uses_backend_session_api():
            return sorted(
                await self._backend_sessions(), key=_session_sort_key, reverse=True
            )

        result = await self._client.list_threads(
            True,
            limit=100,
        )
        raw_threads = extract_threads(result)
        cursor = _next_cursor(result)
        while cursor:
            await self._client.ensure_connected()
            result = await self._client.request(
                "thread/list",
                {
                    "useStateDbOnly": True,
                    "limit": 100,
                    "cursor": cursor,
                    "modelProviders": [],
                },
            )
            raw_threads.extend(extract_threads(result))
            cursor = _next_cursor(result)
        sessions = [
            _session_from_thread(thread, include_turns=False) for thread in raw_threads
        ]
        return sorted(sessions, key=_session_sort_key, reverse=True)

    async def _active_turn_id(self, session_id: str) -> str | None:
        local_turn_id: str | None = None
        async with self._state_lock:
            for turn_id, candidate_session_id in self._turn_to_session.items():
                if candidate_session_id == session_id:
                    local_turn_id = turn_id
                    break

        thread = await self._read_thread(session_id, include_turns=True)
        if thread is not None:
            turn = find_latest_turn(thread, active_only=True)
            if turn and isinstance(turn.get("id"), str):
                return turn["id"]
            if local_turn_id is not None:
                async with self._state_lock:
                    self._forget_turn_locked(local_turn_id)
            return None
        return local_turn_id

    async def _read_thread(
        self,
        session_id: str,
        *,
        include_turns: bool,
    ) -> dict[str, Any] | None:
        if self._uses_backend_session_api():
            read_by_label = getattr(self._client, "read_by_label", None)
            if not callable(read_by_label):
                return None
            try:
                result = await read_by_label(
                    LabelQueryInput(
                        thread_id=session_id,
                        max_items=_thread_history_limit() if include_turns else 5,
                    ),
                    include_turns=include_turns,
                )
            except RuntimeError as exc:
                if _is_thread_unavailable_error(exc):
                    return None
                raise
            except ValueError:
                return None
            thread = _thread_payload(_normalize_backend_thread_payload(result))
            return thread

        fetched_turns = include_turns
        try:
            result = await self._client.read_thread(session_id, include_turns)
        except RuntimeError as exc:
            message = _runtime_error_message(exc).lower()
            if _is_thread_unavailable_error(exc):
                return None
            if include_turns and "includeturns is unavailable" in message:
                result = await self._client.read_thread(session_id, False)
                fetched_turns = False
            elif include_turns and _is_payload_too_large_error(exc):
                logger.warning(
                    "Thread %s full payload is too large; reading compact state",
                    session_id,
                )
                result = await self._client.read_thread(session_id, False)
                fetched_turns = False
            else:
                raise
        except Exception as exc:
            if not include_turns or not _is_payload_too_large_error(exc):
                raise
            logger.warning(
                "Thread %s full payload is too large; reading compact state",
                session_id,
            )
            result = await self._client.read_thread(session_id, False)
            fetched_turns = False
        thread = _thread_payload(result)
        if thread is not None and fetched_turns:
            await _merge_tracked_turn_details(self._client, thread)
        elif thread is not None and include_turns:
            await _merge_tracked_turn_summaries(self._client, thread)
        return thread

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

    def _remember_turn_prompt_locked(self, turn_id: str, prompt: str) -> None:
        self._turn_prompt.setdefault(turn_id, prompt)
        while len(self._turn_prompt) > _TRACKED_TURN_TEXT_LIMIT:
            self._turn_prompt.pop(next(iter(self._turn_prompt)))

    def _remember_turn_steer_locked(self, turn_id: str, steer: SteerInfo) -> None:
        self._turn_steers.setdefault(turn_id, []).append(steer)
        while len(self._turn_steers) > _TRACKED_TURN_TEXT_LIMIT:
            self._turn_steers.pop(next(iter(self._turn_steers)))

    def _forget_turn_locked(self, turn_id: str) -> None:
        # _turn_prompt and _turn_steers survive turn completion on purpose:
        # they keep history display correct while the app-server payload still
        # lacks the turn's userMessage items. They are capped instead.
        self._turn_to_session.pop(turn_id, None)
        self._delivered_text.pop(turn_id, None)
        self._turn_current_item.pop(turn_id, None)
        for item_key in [
            item_key for item_key in self._delivered_item_text if item_key[0] == turn_id
        ]:
            self._delivered_item_text.pop(item_key, None)


_session_manager: CodexAppServerSessionManager | None = None


def _execution_backend_for_configured_backend(backend: str) -> str:
    if backend == OPENBASE_CLOUD_BACKEND:
        return CLAUDE_CODE_BACKEND
    if backend == OPENBASE_CLOUD_CODEX_BACKEND:
        return CODEX_BACKEND
    return backend


def _configured_execution_backend() -> str:
    if DEFAULT_ENV_FILE_PATH.is_file():
        configured_backend = read_backend(DEFAULT_ENV_FILE_PATH)
        if not configured_backend.startswith("unsupported:"):
            return _execution_backend_for_configured_backend(configured_backend)
    return backend_from_environment()


def _default_client_for_execution_backend(
    *,
    manager: CodexAppServerSessionManager,
    ws_url: str,
    execution_backend: str,
) -> _SuperAgentsClient:
    if execution_backend == CLAUDE_CODE_BACKEND:
        from super_agents.claude_sdk import ClaudeAgentSdkClient

        return ClaudeAgentSdkClient()
    return _OpenbaseSuperAgentsClient(manager, ws_url)


def _supports_routine_methods(client: Any) -> bool:
    return all(
        callable(getattr(client, method, None))
        for method in (
            "save_routine",
            "list_routines",
            "read_routine",
            "delete_routine",
            "run_due_routines",
        )
    )


def _find_shared_permission_request(request_id: str | int) -> dict[str, Any] | None:
    request_ids = {str(request_id)}
    if isinstance(request_id, str) and request_id.isdigit():
        request_ids.add(str(int(request_id)))
    for request in shared_permission_requests():
        if str(request.get("id")) in request_ids:
            return request
    return None


def get_session_manager() -> CodexAppServerSessionManager:
    """Get the singleton thread manager instance."""
    global _session_manager
    execution_backend = _configured_execution_backend()
    if (
        _session_manager is None
        or (
            not _session_manager._uses_external_client
            and _session_manager._execution_backend != execution_backend
        )
    ):
        _session_manager = CodexAppServerSessionManager()
    return _session_manager


def _configured_model_for_role(role: str) -> str | None:
    if role == DISPATCHER_MODEL_ROLE:
        return dispatcher_model()
    if role == SUPER_AGENTS_MODEL_ROLE:
        return super_agents_model()
    raise ValueError(f"Unsupported model role: {role}")


def _with_dispatcher_onboarding_reminder(thread_id: str, prompt: str) -> str:
    """Append the onboarding reminder to messages bound for the dispatcher."""
    try:
        state = get_livekit_voice_route_state()
    except Exception:
        return prompt
    if not state.dispatcher_thread_id or state.dispatcher_thread_id != thread_id:
        return prompt
    return append_onboarding_reminder(prompt)


def _has_livekit_voice_route() -> bool:
    try:
        state = get_livekit_voice_route_state()
    except Exception:
        return False
    return bool(state.dispatcher_thread_id or state.active_target_thread_id)
