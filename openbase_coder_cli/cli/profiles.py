"""Install/repair Openbase's session profiles without running full setup."""

from __future__ import annotations

from pathlib import Path

import click

from openbase_coder_cli.agent_profiles import profile_environment
from openbase_coder_cli.cli.setup.claude import _ensure_claude_hooks, _ensure_claude_mcp
from openbase_coder_cli.cli.setup.codex import _ensure_codex_config
from openbase_coder_cli.cli.setup.hooks import (
    ensure_claude_session_id_hook,
    ensure_codex_session_id_hook,
    ensure_session_id_hook_script,
)
from openbase_coder_cli.env_file import (
    selected_backend_from_env_file,
    upsert_env_file_values,
)
from openbase_coder_cli.paths import CLAUDE_SETTINGS_PATH, CODEX_CONFIG_PATH
from openbase_coder_cli.services.registry import require_installation


@click.group()
def profiles() -> None:
    """Manage the configuration layers for Openbase conversations."""


@profiles.command("install")
@click.option(
    "--include-default-hooks",
    is_flag=True,
    help="Also register the session-ID hook in the default Codex and Claude Code configurations.",
)
def install(include_default_hooks: bool) -> None:
    """Install profiles and migrate identifiable legacy user-config entries."""
    config = require_installation()
    backend = selected_backend_from_env_file(Path(config.env_file))
    ensure_session_id_hook_script()
    _ensure_codex_config(config.workspace_path or "", coding_backend=backend)
    _ensure_claude_mcp(config.workspace_path or "", coding_backend=backend)
    _ensure_claude_hooks()
    upsert_env_file_values(Path(config.env_file), profile_environment())
    if include_default_hooks:
        # Profile installation migrates legacy Openbase hooks out of the shared
        # configs, so explicitly requested default hooks must be added afterward.
        ensure_codex_session_id_hook(CODEX_CONFIG_PATH, backup=True)
        ensure_claude_session_id_hook(CLAUDE_SETTINGS_PATH, backup=True)
    click.echo("Installed Openbase profiles. Restart Openbase services to load them.")
