"""Attach the product protocol through generic Super Agents extension points."""

from __future__ import annotations

import inspect
import json

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
    def __init__(self, store, *, protocol=None):
        self.client = None
        self.protocol = protocol or AnnouncementProtocol(
            store, AnnouncementLedger(get_data_dir() / "agent-announcements.sqlite3")
        )

    def eligible(self, session):
        return (
            WORKER_POLICY_MARKER in (session.developer_instructions or "")
            and session.id != get_livekit_voice_route_state().dispatcher_thread_id
        )

    def configure_session(self, session, options, sdk):
        if self.client is not None:
            configure_delegation(options, self.client, self.protocol.ledger, session.id)
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
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        PROTOCOL_INSTRUCTIONS
                        + " Your speaking name is "
                        + str(self.protocol.store.get_session(thread_id).agent_name)
                        + "."
                    ),
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


def managed_claude_client(client_type=None, **kwargs):
    if client_type is None:
        from super_agents.claude_sdk import ClaudeAgentSdkClient

        client_type = ClaudeAgentSdkClient
    parameters = inspect.signature(client_type.__init__).parameters
    if not {"configure_session", "validate_turn_result"}.issubset(parameters):
        raise RuntimeError(
            "Managed worker announcements require Super Agents session extensions. "
            "Update the complete Openbase runtime before starting Claude workers."
        )
    # The same client hosts delegated MCP workers, so they receive this policy
    # without the Dispatcher adding greeting instructions to task prompts.
    from super_agents.agent_store import Store

    store = kwargs.pop("store", None) or Store(backend=kwargs.get("backend_identity"))
    announcements = ManagedAnnouncements(store)
    client = client_type(
        store=store,
        configure_session=announcements.configure_session,
        validate_turn_result=announcements.validate_turn_result,
        **kwargs,
    )
    announcements.client = client
    return client
