"""Mixed-backend session management.

Historically one configured coding backend ruled every thread: the session
manager wrapped a single backend client, so the threads view and the voice
dispatcher only ever saw claude-code OR codex agents. This facade holds one
inner ``CodexAppServerSessionManager`` per configured execution backend and
presents the same surface the app consumes:

- listings merge every backend's threads (each tagged with its ``backend``);
- thread-scoped operations route to whichever backend owns the thread;
- creation targets an explicit backend or the primary (first configured);
- everything else delegates to the primary manager.

``get_session_manager()`` returns this facade when more than one backend is
configured (``OPENBASE_CODING_BACKENDS``), so consumers — the threads API,
approvals, reports, routines, the dispatcher's local-API paths — become
mixed-backend without changes.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from .models import ThreadInfo

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .session_manager import CodexAppServerSessionManager


class MultiBackendSessionManager:
    """Routes the session-manager surface across one manager per backend."""

    def __init__(self, managers: dict[str, CodexAppServerSessionManager]) -> None:
        if not managers:
            raise ValueError("MultiBackendSessionManager needs at least one manager")
        self._managers = dict(managers)
        self._primary_backend = next(iter(self._managers))
        # Learned thread ownership; misses fall back to probing each backend.
        self._thread_backend: dict[str, str] = {}

    # -- introspection used by get_session_manager()'s cache invalidation --

    @property
    def execution_backends(self) -> list[str]:
        return list(self._managers)

    @property
    def _primary(self) -> CodexAppServerSessionManager:
        return self._managers[self._primary_backend]

    def manager_for_backend(self, backend: str) -> CodexAppServerSessionManager | None:
        return self._managers.get(backend)

    def __getattr__(self, name: str) -> Any:
        # Anything not explicitly routed behaves exactly like the primary
        # manager (single-backend behavior), keeping the facade drop-in.
        return getattr(self._primary, name)

    # -- listings: merge every backend --

    async def list_threads(self) -> list[ThreadInfo]:
        results = await asyncio.gather(
            *(manager.list_threads() for manager in self._managers.values()),
            return_exceptions=True,
        )
        merged: list[ThreadInfo] = []
        for backend, result in zip(self._managers, results, strict=True):
            if isinstance(result, BaseException):
                # One backend being down must not blank the other's threads.
                continue
            for thread in result:
                if thread.backend is None:
                    thread.backend = backend
                self._thread_backend[thread.session_id] = thread.backend
            merged.extend(result)
        merged.sort(key=lambda thread: thread.updated_at, reverse=True)
        return merged

    async def list_approval_requests(self) -> list[dict[str, Any]]:
        results = await asyncio.gather(
            *(m.list_approval_requests() for m in self._managers.values()),
            return_exceptions=True,
        )
        merged: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, BaseException):
                continue
            merged.extend(result)
        return merged

    # -- creation: explicit backend or primary --

    async def create_thread(
        self,
        directory: str,
        thread_id: str | None = None,
        backend: str | None = None,
    ) -> ThreadInfo:
        manager = self._managers.get(backend or self._primary_backend, self._primary)
        thread = await manager.create_thread(directory, thread_id)
        owner = backend or self._primary_backend
        if thread.backend is None:
            thread.backend = owner
        self._thread_backend[thread.session_id] = owner
        return thread

    # -- thread-scoped routing --

    async def _manager_for_thread(self, thread_id: str) -> CodexAppServerSessionManager:
        backend = self._thread_backend.get(thread_id)
        if backend in self._managers:
            return self._managers[backend]
        for candidate_backend, manager in self._managers.items():
            try:
                state = await manager.get_session_state(thread_id)
            except Exception:  # noqa: BLE001 - probing; other backends may own it
                continue
            if state is not None:
                self._thread_backend[thread_id] = candidate_backend
                return manager
        # Unknown thread: hand it to the primary so the caller gets that
        # backend's native not-found behavior.
        return self._primary

    async def get_session_state(self, session_id: str) -> ThreadInfo | None:
        manager = await self._manager_for_thread(session_id)
        return await manager.get_session_state(session_id)

    async def get_thread_state(self, thread_id: str) -> ThreadInfo | None:
        return await self.get_session_state(thread_id)

    async def start_turn(self, thread_id: str, prompt: str) -> str:
        manager = await self._manager_for_thread(thread_id)
        return await manager.start_turn(thread_id, prompt)

    async def queue_turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        manager = await self._manager_for_thread(thread_id)
        return await manager.queue_turn(thread_id, prompt)

    async def steer_turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        manager = await self._manager_for_thread(thread_id)
        return await manager.steer_turn(thread_id, prompt)

    async def interrupt_turn(self, thread_id: str) -> bool:
        manager = await self._manager_for_thread(thread_id)
        return await manager.interrupt_turn(thread_id)

    async def send_message(self, session_id: str, message: str) -> str:
        manager = await self._manager_for_thread(session_id)
        return await manager.send_message(session_id, message)

    async def archive_thread(self, thread_id: str) -> bool:
        manager = await self._manager_for_thread(thread_id)
        result = await manager.archive_thread(thread_id)
        self._thread_backend.pop(thread_id, None)
        return result

    async def close_session(self, session_id: str) -> bool:
        manager = await self._manager_for_thread(session_id)
        result = await manager.close_session(session_id)
        self._thread_backend.pop(session_id, None)
        return result

    async def resume_thread_with_developer_instructions(
        self,
        thread_id: str,
        directory: str,
        developer_instructions: str,
    ) -> None:
        manager = await self._manager_for_thread(thread_id)
        await manager.resume_thread_with_developer_instructions(
            thread_id, directory, developer_instructions
        )

    async def resume_thread_without_developer_instructions(
        self,
        thread_id: str,
        directory: str,
    ) -> None:
        manager = await self._manager_for_thread(thread_id)
        await manager.resume_thread_without_developer_instructions(thread_id, directory)
