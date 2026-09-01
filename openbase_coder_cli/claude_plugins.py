"""Claude Code plugin toggles for Openbase sessions.

Claude Code's built-in `computer-use` MCP server is interactive-only and never
attaches to headless Agent SDK sessions, so the Claude backend gets computer
use through an Openbase-provided MCP server instead: a stdio shim
(`openbase-coder claude computer-use-mcp`) that proxies to the desktop app's
control server. Toggling a plugin here adds or removes the corresponding
`mcpServers` entry in the shared ~/.claude.json; new Super Agents sessions
pick the change up on the next dispatcher recreate.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from shutil import which

from openbase_coder_cli.env_file import env_file_values, upsert_env_file_values
from openbase_coder_cli.paths import CLAUDE_STATE_PATH, DEFAULT_ENV_FILE_PATH
from openbase_coder_cli.runtime import current_runtime_package, stable_package_path

COMPUTER_USE_SERVER_NAME = "openbase-computer-use"
COMPUTER_USE_MCP_ARGS = ("claude", "computer-use-mcp")
OPENBASE_CODER_COMMAND = "openbase-coder"
# Read by super-agents when building Claude Agent SDK sessions; a "chrome"
# key adds --chrome so sessions get the Claude in Chrome browser tools.
CHROME_EXTRA_ARGS_ENV_KEY = "SUPER_AGENTS_CLAUDE_EXTRA_ARGS"
CHROME_FLAG = "chrome"


def openbase_coder_command_path() -> Path:
    """Resolve the openbase-coder binary for persisted MCP configs."""
    runtime_package = current_runtime_package()
    if runtime_package is not None:
        bundled = runtime_package.python_path.parent / OPENBASE_CODER_COMMAND
        if bundled.is_file():
            # Persisted into MCP configs: must survive release rotation.
            return stable_package_path(bundled)
    sibling = Path(sys.executable).parent / OPENBASE_CODER_COMMAND
    if sibling.is_file():
        return sibling
    if command := which(OPENBASE_CODER_COMMAND):
        return Path(command)
    return sibling


def computer_use_server_entry() -> dict[str, object]:
    return {
        "type": "stdio",
        "command": str(openbase_coder_command_path()),
        "args": list(COMPUTER_USE_MCP_ARGS),
    }


def computer_use_enabled() -> bool:
    servers = _mcp_servers(_read_json_object(CLAUDE_STATE_PATH))
    return COMPUTER_USE_SERVER_NAME in servers


def set_computer_use_enabled(enabled: bool) -> bool:
    """Add or remove the managed computer-use MCP entry.

    Returns True when the shared config changed. The entry name is
    Openbase-prefixed so it is clearly Openbase-managed in the user's
    ~/.claude.json.
    """
    existing = _read_json_object(CLAUDE_STATE_PATH)
    mcp_servers = dict(_mcp_servers(existing))
    if enabled:
        entry = computer_use_server_entry()
        if mcp_servers.get(COMPUTER_USE_SERVER_NAME) == entry:
            return False
        mcp_servers[COMPUTER_USE_SERVER_NAME] = entry
    else:
        if COMPUTER_USE_SERVER_NAME not in mcp_servers:
            return False
        del mcp_servers[COMPUTER_USE_SERVER_NAME]

    _write_json_object(
        CLAUDE_STATE_PATH, {**existing, "mcpServers": mcp_servers}
    )
    return True


def chrome_enabled() -> bool:
    return CHROME_FLAG in _extra_args_payload()


def set_chrome_enabled(enabled: bool) -> bool:
    """Add or remove the --chrome flag for Claude backend sessions.

    Edits the SUPER_AGENTS_CLAUDE_EXTRA_ARGS JSON in the Openbase .env file,
    preserving any other flags. Returns True when the file changed; a service
    restart is required for sessions to pick it up.
    """
    extra_args = _extra_args_payload()
    if (CHROME_FLAG in extra_args) == enabled:
        return False
    if enabled:
        extra_args[CHROME_FLAG] = None
    else:
        del extra_args[CHROME_FLAG]

    upsert_env_file_values(
        DEFAULT_ENV_FILE_PATH,
        {CHROME_EXTRA_ARGS_ENV_KEY: json.dumps(extra_args) if extra_args else ""},
    )
    return True


def _extra_args_payload() -> dict[str, object]:
    raw = env_file_values(DEFAULT_ENV_FILE_PATH).get(CHROME_EXTRA_ARGS_ENV_KEY, "")
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _mcp_servers(payload: dict[str, object]) -> dict[str, object]:
    mcp_servers = payload.get("mcpServers")
    return mcp_servers if isinstance(mcp_servers, dict) else {}


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_object(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.chmod(0o600)
    tmp_path.replace(path)
