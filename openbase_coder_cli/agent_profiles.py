"""Openbase's profile selection, scoped to its clients and MCP children."""

from __future__ import annotations

from openbase_coder_cli.paths import (
    CLAUDE_PROFILE_MCP_PATH,
    CLAUDE_PROFILE_SETTINGS_PATH,
    CLOUD_CODEX_PROFILE_PATH,
    CODEX_PROFILE_PATH,
)


def profile_environment() -> dict[str, str]:
    return {
        "SUPER_AGENTS_CODEX_PROFILE_PATH": str(CODEX_PROFILE_PATH),
        "SUPER_AGENTS_OPENBASE_CLOUD_CODEX_PROFILE_PATH": str(CLOUD_CODEX_PROFILE_PATH),
        "SUPER_AGENTS_CLAUDE_SETTINGS_PATH": str(CLAUDE_PROFILE_SETTINGS_PATH),
        "SUPER_AGENTS_CLAUDE_MCP_CONFIG_PATH": str(CLAUDE_PROFILE_MCP_PATH),
    }
