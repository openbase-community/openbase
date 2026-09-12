"""Install/repair Openbase's session profiles without running full setup."""

from __future__ import annotations

from pathlib import Path

import click

from openbase_coder_cli.agent_profiles import profile_environment
from openbase_coder_cli.cli.setup.claude import _ensure_claude_hooks, _ensure_claude_mcp
from openbase_coder_cli.cli.setup.codex import _ensure_codex_config
from openbase_coder_cli.cli.setup.hooks import ensure_session_id_hook_script
from openbase_coder_cli.env_file import (
    selected_backend_from_env_file,
    upsert_env_file_values,
)
from openbase_coder_cli.services.registry import require_installation


@click.group()
def profiles() -> None:
    """Manage the configuration layers for Openbase conversations."""


@profiles.command("install")
def install() -> None:
    """Install profiles and migrate identifiable legacy user-config entries."""
    config = require_installation()
    backend = selected_backend_from_env_file(Path(config.env_file))
    ensure_session_id_hook_script()
    _ensure_codex_config(config.workspace_path or "", coding_backend=backend)
    _ensure_claude_mcp(config.workspace_path or "", coding_backend=backend)
    _ensure_claude_hooks()
    upsert_env_file_values(Path(config.env_file), profile_environment())
    click.echo("Installed Openbase profiles. Restart Openbase services to load them.")
