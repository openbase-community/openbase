"""Claude session settings and MCP profiles, sharing the user's login."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import click

from openbase_coder_cli.agent_profiles import profile_environment
from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    DEFAULT_CODING_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    SUPER_AGENTS_DEFAULT_BACKEND_ENV_KEY,
)
from openbase_coder_cli.cli.setup.codex import _super_agents_mcp_command
from openbase_coder_cli.cli.setup.hooks import ensure_claude_session_id_hook
from openbase_coder_cli.cli.setup.profile_migration import (
    migrate_claude_user_config,
    write_if_changed,
)
from openbase_coder_cli.paths import (
    CLAUDE_CONFIG_DIR,
    CLAUDE_PROFILE_MCP_PATH,
    CLAUDE_PROFILE_SETTINGS_PATH,
    CLAUDE_SETTINGS_PATH,
    CLAUDE_STATE_PATH,
    CODEX_AGENTS_MD_PATH,
    CODEX_DISPATCHER_CONFIG_PATH,
    CODEX_SUPER_AGENT_INSTRUCTIONS_PATH,
    OPENBASE_AGENTS_MD_PATH,
)

# Openbase Claude sessions get their system prompt from the rendered Openbase
# instructions file, passed per session by super-agents.
SUPER_AGENTS_BASE_INSTRUCTIONS_ENV = "SUPER_AGENTS_BASE_INSTRUCTIONS_PATH"


def _ensure_claude_md_symlink() -> None:
    """Keep the user's Claude instructions linked to their Codex AGENTS.md."""
    source_path = CODEX_AGENTS_MD_PATH.expanduser()
    target_path = CLAUDE_CONFIG_DIR.expanduser() / "CLAUDE.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if not source_path.exists():
        if (
            target_path.exists()
            and target_path.is_file()
            and not target_path.is_symlink()
        ):
            source_path.write_text(
                target_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        else:
            source_path.touch()

    relative_source = Path(os.path.relpath(source_path, target_path.parent))
    if target_path.is_symlink():
        if target_path.readlink() == relative_source:
            click.echo(f"Claude CLAUDE.md already linked at {target_path}")
            return
        target_path.unlink()
    elif target_path.exists():
        if not target_path.is_file():
            click.echo(
                f"Claude CLAUDE.md already exists at {target_path}; "
                "leaving it unchanged."
            )
            return
        if target_path.read_text(encoding="utf-8") != source_path.read_text(
            encoding="utf-8"
        ):
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = target_path.with_name(
                f"CLAUDE.md.backup-openbase-coder-{timestamp}"
            )
            target_path.replace(backup_path)
            click.echo(f"Backed up Claude CLAUDE.md to {backup_path}")
        else:
            target_path.unlink()

    target_path.symlink_to(relative_source)
    click.echo(f"Linked Claude CLAUDE.md at {target_path}")


def _ensure_claude_mcp(
    workspace_dir: str,
    *,
    coding_backend: str = DEFAULT_CODING_BACKEND,
) -> None:
    """Register the super-agents MCP server in Openbase's Claude profile."""
    command_path, args = _super_agents_mcp_command(Path(workspace_dir))
    if not command_path.is_file():
        click.echo(
            f"Super Agents MCP command not found at {command_path}; "
            "writing the expected Claude MCP config path anyway."
        )

    existing = _read_json_object(CLAUDE_PROFILE_MCP_PATH)
    mcp_servers = existing.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
    entry = {
        "type": "stdio",
        "command": str(command_path),
        **({"args": args} if args else {}),
        "env": {
            **profile_environment(),
            "SUPER_AGENTS_DEFAULT_CONFIG_PATH": str(CODEX_DISPATCHER_CONFIG_PATH),
            "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH": str(
                CODEX_SUPER_AGENT_INSTRUCTIONS_PATH
            ),
            SUPER_AGENTS_BASE_INSTRUCTIONS_ENV: str(OPENBASE_AGENTS_MD_PATH),
            SUPER_AGENTS_DEFAULT_BACKEND_ENV_KEY: (
                OPENBASE_CLOUD_BACKEND
                if coding_backend == OPENBASE_CLOUD_BACKEND
                else CLAUDE_CODE_BACKEND
            ),
        },
    }
    if mcp_servers.get("super-agents") == entry:
        click.echo(
            f"Claude profile already has super-agents at {CLAUDE_PROFILE_MCP_PATH}"
        )
        return

    updated = {**existing, "mcpServers": {**mcp_servers, "super-agents": entry}}
    write_if_changed(
        CLAUDE_PROFILE_MCP_PATH,
        json.dumps(updated, indent=2, sort_keys=True) + "\n",
    )
    click.echo(
        f"Registered super-agents MCP in Claude profile {CLAUDE_PROFILE_MCP_PATH}"
    )


def _ensure_claude_hooks() -> None:
    """Configure Openbase's settings layer before removing legacy entries."""
    settings = _read_json_object(CLAUDE_PROFILE_SETTINGS_PATH)
    settings.setdefault("model", "sonnet")
    settings.setdefault("effortLevel", "high")
    settings.setdefault("permissions", {}).setdefault(
        "defaultMode", "bypassPermissions"
    )
    write_if_changed(
        CLAUDE_PROFILE_SETTINGS_PATH, json.dumps(settings, indent=2) + "\n"
    )
    ensure_claude_session_id_hook(CLAUDE_PROFILE_SETTINGS_PATH)
    migrate_claude_user_config(CLAUDE_STATE_PATH, CLAUDE_SETTINGS_PATH)


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload
