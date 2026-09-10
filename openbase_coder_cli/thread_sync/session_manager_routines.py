"""Super Agents routine dispatch mixin for the session manager.

`SessionManagerRoutinesMixin` groups the persisted-routine CRUD/run methods and
the routine-client resolution. Pure structural extraction; every method is
unchanged and reaches sibling state through ``self``.
"""

from __future__ import annotations

from typing import Any

from .session_manager_base import (
    _OpenbaseSuperAgentsClient,
    _RoutineClient,
    _supports_routine_methods,
)


class SessionManagerRoutinesMixin:
    """Persisted Super Agents routine dispatch."""

    def _routines_client(self) -> _RoutineClient:
        if self._routine_client is not None:
            return self._routine_client
        if _supports_routine_methods(self._client):
            self._routine_client = self._client
        else:
            self._routine_client = _OpenbaseSuperAgentsClient(self, self._ws_url)
        return self._routine_client

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

    async def add_routine_trigger(
        self, name: str, trigger_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Add a webhook trigger to a persisted routine (loop)."""
        return await self._routines_client().add_routine_trigger(name, trigger_input)

    async def remove_routine_trigger(
        self, name: str, trigger_id: str
    ) -> dict[str, Any]:
        """Remove a trigger from a persisted routine (loop)."""
        return await self._routines_client().remove_routine_trigger(name, trigger_id)

    async def deliver_webhook_event(
        self,
        token: str,
        *,
        headers: dict[str, Any] | None = None,
        body: bytes | str = b"",
        origin: str = "external",
    ) -> dict[str, Any]:
        """Deliver an inbound webhook event to the loop trigger owning the token."""
        return await self._routines_client().deliver_webhook_event(
            token, headers=headers, body=body, origin=origin
        )

    async def emit_routine_event(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a loop immediately with a locally emitted event payload."""
        return await self._routines_client().emit_routine_event(name, payload, event_id)
