"""Install/repair Openbase's session profiles without running full setup."""

from __future__ import annotations

from pathlib import Path

import click

from openbase_coder_cli.agent_profiles import profile_environment
from openbase_coder_cli.cli.setup.claude import _ensure_claude_hooks, _ensure_claude_mcp
from openbase_coder_cli.cli.setup.codex import _ensure_codex_config
from openbase_coder_cli.cli.setup.hooks import (
    ensure_default_session_id_hooks,
    ensure_session_id_hook_script,
    include_default_hooks_option,
)
from openbase_coder_cli.env_file import (
    selected_backend_from_env_file,
    upsert_env_file_values,
)
from openbase_coder_cli.services.registry import require_installation


@click.group()
def profiles() -> None:
    """Manage the configuration layers for Openbase conversations."""


@profiles.command("install")
@include_default_hooks_option
@click.option(
    "--shared-super-agents-mcp/--no-shared-super-agents-mcp",
    "shared_super_agents_mcp",
    default=True,
    show_default=True,
    help=(
        "Also register the Super Agents MCP in the default (non-Openbase) "
        "Codex and Claude Code homes so plain terminal codex/claude sessions "
        "can dispatch Super Agents (a pre-existing entry is left untouched). "
        "Pass --no-shared-super-agents-mcp to strip it from those homes."
    ),
)
def install(include_default_hooks: bool, shared_super_agents_mcp: bool) -> None:
    """Install profiles and migrate identifiable legacy user-config entries."""
    config = require_installation()
    backend = selected_backend_from_env_file(Path(config.env_file))
    workspace_dir = config.workspace_path or ""
    ensure_session_id_hook_script()
    _ensure_codex_config(
        workspace_dir,
        coding_backend=backend,
        register_shared_super_agents=shared_super_agents_mcp,
    )
    _ensure_claude_mcp(workspace_dir, coding_backend=backend)
    _ensure_claude_hooks(
        register_shared_super_agents=shared_super_agents_mcp,
        workspace_dir=workspace_dir,
        coding_backend=backend,
    )
    upsert_env_file_values(Path(config.env_file), profile_environment())
    if include_default_hooks:
        ensure_default_session_id_hooks()
    click.echo("Installed Openbase profiles. Restart Openbase services to load them.")
