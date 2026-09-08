"""Shared helpers, constants and the app-server client for the session manager.

`CodexAppServerSessionManager` is assembled from mixins that live in sibling
modules (`session_manager_threads`, `session_manager_turns`,
`session_manager_routines`, `session_manager_approvals`). Those mixins and the
top-level `session_manager` module all need the same free helpers, protocol
definitions, constants and the `_OpenbaseSuperAgentsClient` subclass. Keeping
them here lets every piece import them without importing the top-level module
(which would be circular). The logger name is pinned to the session_manager
dotted path so emitted log lines are byte-identical to when everything lived in
one file.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from super_agents.app_models import LabelQueryInput
from super_agents.app_server_client import (
    CodexAppServerClient,
    shared_permission_requests,
)
from super_agents.backend_clients import CLAUDE_CODE_BACKEND

from openbase_coder_cli.dispatcher_config import (
    DISPATCHER_MODEL_ROLE,
    SUPER_AGENTS_MODEL_ROLE,
    dispatcher_model,
    super_agents_model,
)
from openbase_coder_cli.livekit_voice_route import (
    get_livekit_voice_route_state,
)
from openbase_coder_cli.onboarding_reminder import append_onboarding_reminder
from openbase_coder_cli.paths import (
    CODEX_SUPER_AGENT_INSTRUCTIONS_PATH,
)

from .models import ThreadInfo as SessionInfo

logger = logging.getLogger("openbase_coder_cli.thread_sync.session_manager")

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


async def _ensure_client_connected(client: Any) -> None:
    """Connect app-server clients while allowing local SDK backends.

    Codex app-server clients expose ``ensure_connected``. Local backends such
    as ``ClaudeAgentSdkClient`` do not have a transport to connect, so their
    in-process approval queues are ready immediately.
    """
    ensure_connected = getattr(client, "ensure_connected", None)
    if callable(ensure_connected):
        await ensure_connected()


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
    async def add_routine_trigger(self, name: str, trigger_input: dict[str, Any]) -> dict[str, Any]: ...
    async def remove_routine_trigger(self, name: str, trigger_id: str) -> dict[str, Any]: ...
    async def deliver_webhook_event(
        self,
        token: str,
        *,
        headers: dict[str, Any] | None = None,
        body: bytes | str = b"",
        origin: str = "external",
    ) -> dict[str, Any]: ...
    async def emit_routine_event(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]: ...


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


def _default_client_for_execution_backend(
    *,
    manager: "CodexAppServerSessionManager",
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
            "add_routine_trigger",
            "remove_routine_trigger",
            "deliver_webhook_event",
            "emit_routine_event",
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
