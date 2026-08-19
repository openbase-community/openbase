"""Openbase-managed Super Agents MCP entry point with mandatory controls."""

from __future__ import annotations

import asyncio

import click
from super_agents.backend_clients import multi_client_from_environment
from super_agents.mcp_server import run_stdio

from openbase_coder_cli.voice_lockdown.execution_controls import (
    managed_execution_controls,
)


@click.command("super-agents-mcp", hidden=True)
def super_agents_mcp() -> None:
    """Run Super Agents with Openbase's required execution controls."""
    client = multi_client_from_environment(**managed_execution_controls())
    asyncio.run(run_stdio(client))
