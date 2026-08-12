"""Thread lifecycle, listing, reading and resume mixin for the session manager.

`SessionManagerThreadsMixin` groups the create/close/list/read/resume side of
`CodexAppServerSessionManager`. Pure structural extraction; every method is
unchanged and reaches sibling state through ``self``.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from super_agents.app_models import LabelQueryInput
from super_agents.app_server_client import (
    extract_threads,
    login_shell_config_override,
)

from openbase_coder_cli.codex_session_defaults import codex_permission_defaults
from openbase_coder_cli.dispatcher_config import (
    DISPATCHER_MODEL_ROLE,
    SUPER_AGENTS_MODEL_ROLE,
)

from .models import ThreadInfo as SessionInfo
from .session_manager_base import (
    _USE_SUPER_AGENT_INSTRUCTIONS,
    ThreadListPage,
    _is_payload_too_large_error,
    _is_thread_unavailable_error,
    _OpenbaseSuperAgentsClient,
    _runtime_error_message,
    load_super_agent_developer_instructions,
    logger,
)
from .thread_payloads import (
    _datetime_to_iso,
    _merge_tracked_turn_details,
    _merge_tracked_turn_summaries,
    _next_cursor,
    _normalize_backend_thread_payload,
    _session_from_thread,
    _session_sort_key,
    _thread_history_limit,
    _thread_payload,
)


class SessionManagerThreadsMixin:
    """Thread lifecycle, listing, reading and resume."""

    # The Codex app-server returns thread/list pages in thread-creation
    # order, so recency ranking has to fetch the whole recent window and
    # re-sort by update time; sorting single pages would leave an
    # old-but-recently-active thread buried at its creation position.
    RECENCY_WINDOW_THREADS = 500
    RECENCY_FETCH_PAGE_SIZE = 100

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

    async def get_thread_state(self, thread_id: str) -> SessionInfo | None:
        """Get the current thread snapshot."""
        return await self.get_session_state(thread_id)

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

    async def get_session_state(self, session_id: str) -> SessionInfo | None:
        """Get the current thread snapshot."""
        result = await self._read_thread(session_id, include_turns=True)
        if result is None:
            return None
        session = _session_from_thread(result, include_turns=True)
        await self._apply_local_turn_state(session_id, session)
        return session

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
