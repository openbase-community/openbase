"""Claude config phase: Openbase-managed Claude Code config, settings, and auth."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import click

from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    DEFAULT_CODING_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    SUPER_AGENTS_DEFAULT_BACKEND_ENV_KEY,
)
from openbase_coder_cli.claude_auth import (
    claude_auth_status,
    copy_normal_claude_keychain,
    run_claude_login,
    sync_normal_claude_state,
)
from openbase_coder_cli.cli.setup.codex import _super_agents_mcp_command
from openbase_coder_cli.cli.setup.hooks import merge_session_id_hook_into_claude_hooks
from openbase_coder_cli.paths import (
    CODEX_DISPATCHER_CONFIG_PATH,
    CODEX_SUPER_AGENT_INSTRUCTIONS_PATH,
    NORMAL_CLAUDE_CONFIG_DIR,
    NORMAL_CLAUDE_SETTINGS_PATH,
    NORMAL_CLAUDE_STATE_PATH,
    NORMAL_CODEX_AGENTS_MD_PATH,
    OPENBASE_CLAUDE_CONFIG_DIR,
    OPENBASE_CLAUDE_JSON_PATH,
    OPENBASE_CLAUDE_SETTINGS_PATH,
)

CLAUDE_CODE_PERMISSION_MODE = "bypassPermissions"
OPENBASE_CLAUDE_SETTINGS_DEFAULTS = {
    "env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "0"},
    "permissions": {
        "allow": [],
        "deny": [],
        "defaultMode": CLAUDE_CODE_PERMISSION_MODE,
    },
    "skipDangerousModePermissionPrompt": True,
    "skipAutoPermissionPrompt": True,
}


def _ensure_normal_claude_md_symlink() -> None:
    """Keep the user's normal Claude instructions linked to normal Codex AGENTS.md."""
    source_path = NORMAL_CODEX_AGENTS_MD_PATH.expanduser()
    target_path = NORMAL_CLAUDE_CONFIG_DIR.expanduser() / "CLAUDE.md"
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
            click.echo(f"Normal Claude CLAUDE.md already linked at {target_path}")
            return
        target_path.unlink()
    elif target_path.exists():
        if not target_path.is_file():
            click.echo(
                f"Normal Claude CLAUDE.md already exists at {target_path}; "
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
            click.echo(f"Backed up normal Claude CLAUDE.md to {backup_path}")
        else:
            target_path.unlink()

    target_path.symlink_to(relative_source)
    click.echo(f"Linked normal Claude CLAUDE.md at {target_path}")


def _ensure_claude_config(
    workspace_dir: str,
    *,
    coding_backend: str = DEFAULT_CODING_BACKEND,
    link_claude_config: bool = False,
) -> None:
    """Configure Openbase's Claude Code config dir."""
    OPENBASE_CLAUDE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if link_claude_config:
        _symlink_claude_settings()
    _ensure_claude_settings()
    command_path, args = _super_agents_mcp_command(Path(workspace_dir))
    if not command_path.is_file():
        click.echo(
            f"Super Agents MCP command not found at {command_path}; "
            "writing the expected Claude MCP config path anyway."
        )

    existing = _read_json_object(OPENBASE_CLAUDE_JSON_PATH)
    mcp_servers = existing.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
    updated = {
        **existing,
        "mcpServers": {
            **mcp_servers,
            "super-agents": {
                "type": "stdio",
                "command": str(command_path),
                **({"args": args} if args else {}),
                "env": {
                    "CLAUDE_CONFIG_DIR": str(OPENBASE_CLAUDE_CONFIG_DIR),
                    "SUPER_AGENTS_DEFAULT_CONFIG_PATH": str(
                        CODEX_DISPATCHER_CONFIG_PATH
                    ),
                    "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH": str(
                        CODEX_SUPER_AGENT_INSTRUCTIONS_PATH
                    ),
                    SUPER_AGENTS_DEFAULT_BACKEND_ENV_KEY: (
                        OPENBASE_CLOUD_BACKEND
                        if coding_backend == OPENBASE_CLOUD_BACKEND
                        else CLAUDE_CODE_BACKEND
                    ),
                },
            },
        },
    }
    if updated == existing:
        click.echo(f"Claude config already configured at {OPENBASE_CLAUDE_JSON_PATH}")
        return

    OPENBASE_CLAUDE_JSON_PATH.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    click.echo(f"Configured Claude MCP config at {OPENBASE_CLAUDE_JSON_PATH}")


def _ensure_normal_claude_mcp(workspace_dir: str) -> None:
    """Register the super-agents MCP server in the user's normal Claude home.

    Only the mcpServers entry — normal Claude settings and permissions are
    never touched. Users can remove the entry; an explicit setup re-run
    restores it.
    """
    command_path, args = _super_agents_mcp_command(Path(workspace_dir))
    existing = _read_json_object(NORMAL_CLAUDE_STATE_PATH)
    mcp_servers = existing.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
    entry = {
        "type": "stdio",
        "command": str(command_path),
        **({"args": args} if args else {}),
        "env": {SUPER_AGENTS_DEFAULT_BACKEND_ENV_KEY: CLAUDE_CODE_BACKEND},
    }
    if mcp_servers.get("super-agents") == entry:
        click.echo(
            f"Normal Claude config already has super-agents at "
            f"{NORMAL_CLAUDE_STATE_PATH}"
        )
        return

    updated = {**existing, "mcpServers": {**mcp_servers, "super-agents": entry}}
    NORMAL_CLAUDE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = NORMAL_CLAUDE_STATE_PATH.with_name(
        f"{NORMAL_CLAUDE_STATE_PATH.name}.tmp.{os.getpid()}"
    )
    tmp_path.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.chmod(0o600)
    tmp_path.replace(NORMAL_CLAUDE_STATE_PATH)
    click.echo(
        f"Registered super-agents MCP in normal Claude config "
        f"{NORMAL_CLAUDE_STATE_PATH}"
    )


def _symlink_claude_settings() -> None:
    """Point the service Claude settings at the user's normal settings file.

    Mirror of the Codex config share: Openbase's full-permission settings are
    then written through the symlink into the shared ~/.claude/settings.json,
    which affects normal Claude Code sessions.
    """
    service_settings = OPENBASE_CLAUDE_SETTINGS_PATH
    normal_settings = NORMAL_CLAUDE_SETTINGS_PATH

    service_settings.parent.mkdir(parents=True, exist_ok=True)
    normal_settings.parent.mkdir(parents=True, exist_ok=True)

    if normal_settings.exists() and not normal_settings.is_file():
        raise click.ClickException(
            f"Normal Claude settings exists but is not a file: {normal_settings}"
        )

    if service_settings.is_symlink():
        if service_settings.resolve() == normal_settings.resolve():
            click.echo(f"Claude settings already linked to {normal_settings}")
            return
        service_settings.unlink()
    elif service_settings.exists():
        if not service_settings.is_file():
            raise click.ClickException(
                f"Claude settings exists but is not a file: {service_settings}"
            )
        if not normal_settings.exists():
            normal_settings.write_text(
                service_settings.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        service_settings.unlink()

    if not normal_settings.exists():
        normal_settings.write_text("{}\n", encoding="utf-8")

    service_settings.symlink_to(normal_settings)
    click.echo(f"Symlinked Claude settings -> {normal_settings}")


def _ensure_claude_settings() -> None:
    """Configure Claude Code settings for Openbase-managed SDK sessions."""
    existing = _read_json_object(OPENBASE_CLAUDE_SETTINGS_PATH)
    seed = existing or _read_json_object(NORMAL_CLAUDE_SETTINGS_PATH)
    updated = _merge_claude_settings(seed)
    if updated == existing:
        click.echo(
            f"Claude settings already configured at {OPENBASE_CLAUDE_SETTINGS_PATH}"
        )
        return

    OPENBASE_CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPENBASE_CLAUDE_SETTINGS_PATH.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    click.echo(f"Configured Claude settings at {OPENBASE_CLAUDE_SETTINGS_PATH}")


def _merge_claude_settings(settings: dict[str, object]) -> dict[str, object]:
    updated = dict(settings)
    env = updated.get("env")
    if not isinstance(env, dict):
        env = {}
    updated["env"] = {
        **OPENBASE_CLAUDE_SETTINGS_DEFAULTS["env"],
        **env,
    }

    permissions = updated.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    updated["permissions"] = {
        "allow": permissions.get("allow", []),
        "deny": permissions.get("deny", []),
        **{
            key: value
            for key, value in permissions.items()
            if key not in {"allow", "deny", "defaultMode"}
        },
        "defaultMode": CLAUDE_CODE_PERMISSION_MODE,
    }
    updated["skipDangerousModePermissionPrompt"] = True
    updated["skipAutoPermissionPrompt"] = True
    updated["claudeMdExcludes"] = _merge_claude_md_excludes(
        updated.get("claudeMdExcludes")
    )
    updated["hooks"] = merge_session_id_hook_into_claude_hooks(updated.get("hooks"))
    return updated


def _merge_claude_md_excludes(value: object) -> list[str]:
    excludes = (
        [item for item in value if isinstance(item, str)]
        if isinstance(value, list)
        else []
    )
    normal_claude_md_path = str((NORMAL_CLAUDE_CONFIG_DIR / "CLAUDE.md").expanduser())
    if normal_claude_md_path not in excludes:
        excludes.append(normal_claude_md_path)
    return excludes


def _ensure_claude_auth_bridge(
    *, login_if_needed: bool = False, required: bool = True
) -> None:
    """Prepare Openbase's managed Claude Code auth state."""
    result = sync_normal_claude_state()
    if result.state_updated:
        click.echo("Updated Openbase Claude Code state.")
    click.echo(result.message)
    if copy_normal_claude_keychain():
        click.echo("Copied normal Claude Code login into Openbase's keychain entry.")

    status = claude_auth_status()
    if status.logged_in:
        click.echo("Openbase Claude Code auth already configured.")
        return

    if login_if_needed:
        click.echo("Running Claude Code login for Openbase's CLAUDE_CONFIG_DIR...")
        exit_code = run_claude_login()
        if exit_code != 0:
            raise click.ClickException(
                "Claude Code login failed. Run `openbase-coder claude login` and then "
                "`openbase-coder restart --recreate-dispatcher`."
            )
        status = claude_auth_status()
        if status.logged_in:
            click.echo("Openbase Claude Code auth configured.")
            return
        raise click.ClickException(
            "Claude Code login completed but Openbase Claude Code auth is still not "
            "available. Run `openbase-coder claude status` for details."
        )

    if required:
        click.echo(
            "Openbase Claude Code auth is not configured yet. "
            "Run `openbase-coder claude login` before using the Claude Code backend."
        )


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
