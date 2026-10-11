"""Record request origin at the delegation boundary, never from task wording."""

from __future__ import annotations

from super_agents.claude_inprocess_mcp import replace_super_agents_stdio_server


class DelegatingClient:
    """The regular MCP surface, with durable provenance for delegated turns."""

    def __init__(self, client, ledger, parent_id):
        self.client = client
        self.ledger = ledger
        self.parent_id = parent_id

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def _invoke(self, method, query, *args):
        # Resolve with the backend's own ID/name rules. Mark an existing active
        # turn before steering can deliver input to its already running SDK.
        session = self.client._resolve_session(query)
        if (
            method != "queue_turn_by_label"
            and session.active_turn_id
            and (not query.turn_id or query.turn_id == session.active_turn_id)
        ):
            self.ledger.mark_delegated(session.active_turn_id, self.parent_id)
        result = await getattr(self.client, method)(query, *args)
        turn_id = result.get("turnId")
        if turn_id:
            # start_turn creates its asyncio task without yielding. This write
            # therefore precedes its first SDK query, including a newly created
            # worker's first turn. Queued turns retain the same ID when drained.
            self.ledger.mark_delegated(turn_id, self.parent_id)
        return result

    async def start_turn_by_label(self, query, turn_input):
        return await self._invoke("start_turn_by_label", query, turn_input)

    async def steer_by_label(self, query, prompt, turn_input=None):
        return await self._invoke("steer_by_label", query, prompt, turn_input)

    async def queue_turn_by_label(self, query, turn_input):
        return await self._invoke("queue_turn_by_label", query, turn_input)


def configure_delegation(options, client, ledger, parent_id):
    replace_super_agents_stdio_server(
        options, client=DelegatingClient(client, ledger, parent_id)
    )
