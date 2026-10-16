"""Attach the product protocol through generic Super Agents extension points."""

from __future__ import annotations

import json

from super_agents.claude_system_prompt import (
    compose_system_prompt,
    supports_refreshable_system_prompt,
)

from openbase_coder_cli.cli.utils import get_data_dir
from openbase_coder_cli.livekit_voice_route import get_livekit_voice_route_state

from .delegation import configure_delegation
from .ledger import AnnouncementLedger
from .protocol import PROTOCOL_INSTRUCTIONS, AnnouncementProtocol

WORKER_POLICY_MARKER = "You are an Openbase Super Agent."
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "phase": {"type": "string", "enum": ["begin", "finish"]},
        "delivery": {"type": "string", "enum": ["audible", "quiet"]},
        "quiet_request": {
            "type": "string",
            "description": "Exact user instruction requesting silence, when quiet.",
        },
        "resume_request": {
            "type": "string",
            "description": "Exact new user instruction lifting a previous quiet request.",
        },
        "summary": {
            "type": "string",
            "description": "At finish, your name and the actual verified result. No promises or raw logs.",
        },
    },
    "required": ["phase"],
    "additionalProperties": False,
}


class ManagedAnnouncements:
    def __init__(self, store, *, protocol=None, delegation_client=None):
        self.client = None
        # The client a session's in-process super_agents MCP server is bound
        # to. By default the managed client itself (so delegated turns run in
        # this process); the voice job substitutes a forwarding client, so the
        # turns its Dispatcher starts are owned by django-cli and survive the
        # call ending.
        self.delegation_client = delegation_client
        self.protocol = protocol or AnnouncementProtocol(
            store, AnnouncementLedger(get_data_dir() / "agent-announcements.sqlite3")
        )

    def eligible(self, session):
        """Managed workers only: never the Dispatcher, current or retired.

        Exempting the Dispatcher by thread id alone named a recreated
        Dispatcher with its voice ("Jacqueline") before the route state
        carried the new id, and the retired one once the id moved on (376,
        2026-10-11); the identity check covers the label too.
        """
        from openbase_coder_cli.livekit_voice_route import is_dispatcher_identity

        return WORKER_POLICY_MARKER in (
            session.developer_instructions or ""
        ) and not is_dispatcher_identity(
            session.id,
            getattr(session, "name", None),
            getattr(session, "agent_name", None),
            route_state=get_livekit_voice_route_state(),
        )

    def configure_session(self, session, options, sdk):
        delegation_client = self.delegation_client or self.client
        if delegation_client is not None:
            configure_delegation(
                options, delegation_client, self.protocol.ledger, session.id
            )
        if not self.eligible(session):
            return
        if not session.agent_name:
            from openbase_coder_cli.livekit_voice_route import (
                super_agent_voice_for_context,
            )

            voice = super_agent_voice_for_context(session.id, session.name, None)
            if voice is None:
                raise RuntimeError(
                    "Managed worker requires a stable speaking identity."
                )
            self.protocol.store.update_session(session.id, agent_name=voice.name)
        thread_id = session.id
        if not supports_refreshable_system_prompt(sdk):
            raise RuntimeError(
                "Managed announcements require the pinned refreshable Claude SDK."
            )
        identity = (
            " Your speaking name is "
            + str(self.protocol.store.get_session(thread_id).agent_name)
            + "."
        )
        options.system_prompt = compose_system_prompt(
            options.system_prompt or {"type": "preset", "preset": "claude_code"},
            PROTOCOL_INSTRUCTIONS + identity,
            sdk,
        )

        async def task_announcement(arguments):
            # The installed SDK turns handler exceptions into MCP error results.
            # Do not return an apparent successful receipt for a rejected phase.
            result = await self.protocol.announce(thread_id, arguments)
            return {"content": [{"type": "text", "text": json.dumps(result)}]}

        async def pre(event, tool_use_id, context):
            return await self.protocol.before_tool(thread_id, event)

        async def post(event, tool_use_id, context):
            return await self.protocol.after_tool(thread_id, event)

        async def stop(event, tool_use_id, context):
            return await self.protocol.stop(thread_id)

        async def prompt(event, tool_use_id, context):
            current = await self.protocol.context(thread_id)
            origin = "direct conversation" if current.direct else "delegated task"
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": f"This request's recorded origin is {origin}."
                    + identity,
                }
            }

        options.hooks = {
            key: list(value) for key, value in (options.hooks or {}).items()
        }
        for event, callback in {
            "PreToolUse": pre,
            "PostToolUse": post,
            "PostToolUseFailure": post,
            "Stop": stop,
            "UserPromptSubmit": prompt,
        }.items():
            options.hooks.setdefault(event, []).append(
                sdk.HookMatcher(hooks=[callback])
            )
        definition = sdk.tool("task_announcement", PROTOCOL_INSTRUCTIONS, TOOL_SCHEMA)(
            task_announcement
        )
        options.mcp_servers = {
            **(options.mcp_servers or {}),
            "openbase_agent": sdk.create_sdk_mcp_server(
                "openbase_agent", tools=[definition]
            ),
        }

    async def validate_turn_result(self, session, turn, result):
        if self.eligible(session):
            await self.protocol.validate_turn_result(session, turn, result)


def managed_claude_client(
    client_type=None, *, delegation_client_factory=None, **kwargs
):
    """Build the managed Claude client.

    ``delegation_client_factory(client)`` returns the client that the
    sessions' in-process super_agents MCP server is bound to, when delegated
    turns must run somewhere other than this process (the voice job forwards
    them to django-cli; see ``openbase_coder_cli.super_agents_remote``).
    """
    if client_type is None:
        from super_agents.claude_sdk import ClaudeAgentSdkClient

        client_type = ClaudeAgentSdkClient
    if getattr(client_type, "embedding_callbacks_version", 0) < 1:
        raise RuntimeError(
            "Managed worker announcements require Super Agents session extensions. "
            "Update the complete Openbase runtime before starting Claude workers."
        )
    # The same client hosts delegated MCP workers, so they receive this policy
    # without the Dispatcher adding greeting instructions to task prompts.
    from super_agents.agent_store import Store

    store = kwargs.pop("store", None) or Store(backend=kwargs.get("backend_identity"))
    announcements = ManagedAnnouncements(store)
    from openbase_coder_cli.container_turns import container_client_type

    client = container_client_type(client_type)(
        store=store,
        configure_session=announcements.configure_session,
        validate_turn_result=announcements.validate_turn_result,
        **kwargs,
    )
    announcements.client = client
    if delegation_client_factory is not None:
        announcements.delegation_client = delegation_client_factory(client)
    return client
