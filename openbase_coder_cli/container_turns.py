"""Memory admission and prompt-process lifetime for container Claude workers."""

from __future__ import annotations

from contextlib import asynccontextmanager

from openbase_coder_cli.workspace_memory import acquire_turn_slot, is_container


class ContainerClaudeTurns:
    async def warm_session_by_label(self, input_data, turn_input=None):
        # No speculative CLI at call setup. The first real turn owns its
        # memory reservation and starts the process on demand.
        session = self._resolve_session(input_data)
        return self._warm_result(session, warmed=False, reason="container_on_demand")

    def _memory_leases(self):
        if not hasattr(self, "_workspace_memory_leases"):
            self._workspace_memory_leases = {}
        return self._workspace_memory_leases

    def _reserve_memory(self, session_id):
        leases = self._memory_leases()
        if session_id not in leases:
            leases[session_id] = acquire_turn_slot()

    def _release_memory(self, session_id):
        lease = self._memory_leases().pop(session_id, None)
        if lease is not None:
            lease.close()

    def _launch_turn(self, session, *args, **kwargs):
        self._reserve_memory(session.id)
        try:
            return super()._launch_turn(session, *args, **kwargs)
        except BaseException:
            self._release_memory(session.id)
            raise

    async def _sdk_client_for(self, session, *args, **kwargs):
        # Queue drains also enter here, inside the SDK's persisted-error path.
        self._reserve_memory(session.id)
        return await super()._sdk_client_for(session, *args, **kwargs)

    @asynccontextmanager
    async def _cross_process_session_lock(self, session_id):
        async with super()._cross_process_session_lock(session_id):
            try:
                yield
            finally:
                # Flush/close before releasing the session lock: a queued or
                # cross-process resume must not race an old CLI's final writes.
                try:
                    await self._disconnect_sdk_client(session_id)
                finally:
                    self._release_memory(session_id)

    async def _run_turn(self, session_id, *args, **kwargs):
        try:
            return await super()._run_turn(session_id, *args, **kwargs)
        finally:
            # Also covers cancellation while waiting to acquire session locks.
            self._release_memory(session_id)


def container_client_type(client_type):
    if not is_container():
        return client_type
    return type("ContainerClaudeClient", (ContainerClaudeTurns, client_type), {})
