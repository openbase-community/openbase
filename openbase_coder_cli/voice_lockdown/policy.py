"""Safe-baseline preflight; lockdown never repairs an unsafe baseline."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from openbase_coder_cli.codex_session_defaults import codex_permission_defaults
from openbase_coder_cli.paths import (
    CODEX_HOME_DIR,
    DEFAULT_ENV_FILE_PATH,
    OPENBASE_CLAUDE_JSON_PATH,
)

UNSAFE_CODEX_APPROVAL_POLICIES = {"never"}
UNSAFE_CODEX_SANDBOXES = {"danger-full-access", "dangerfullaccess"}
UNSAFE_CLAUDE_PERMISSION_MODES = {"bypasspermissions", "dontask"}


@dataclass(frozen=True, slots=True)
class BaselineCheck:
    safe: bool
    reasons: tuple[str, ...]
    configured_backends: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagedControlsCheck:
    ready: bool
    reasons: tuple[str, ...]


def check_safe_baseline(env: dict[str, str] | None = None) -> BaselineCheck:
    values = (
        {
            **{key: value for key, value in dotenv_values(DEFAULT_ENV_FILE_PATH).items() if value is not None},
            **os.environ,
        }
        if env is None
        else env
    )
    raw_backends = values.get("OPENBASE_CODING_BACKEND", "codex")
    configured = tuple(
        dict.fromkeys(
            part.strip().lower().replace("-", "_").replace(" ", "_")
            for part in raw_backends.split(",")
            if part.strip()
        )
    ) or ("codex",)
    reasons: list[str] = []
    permissions = codex_permission_defaults(values)
    for backend in configured:
        if backend in {"codex", "openbase_cloud_codex"}:
            approval = permissions["approvalPolicy"].strip().lower()
            sandbox = permissions["sandbox"].strip().lower().replace("_", "-")
            if approval in UNSAFE_CODEX_APPROVAL_POLICIES:
                reasons.append(f"{backend} approval policy is unsafe: {approval}")
            if sandbox.replace("-", "") in UNSAFE_CODEX_SANDBOXES or sandbox in UNSAFE_CODEX_SANDBOXES:
                reasons.append(f"{backend} sandbox is unsafe: {sandbox}")
            if approval not in {"untrusted", "on-failure", "on-request"}:
                reasons.append(f"{backend} approval policy is unknown: {approval}")
            if sandbox not in {"read-only", "workspace-write"}:
                reasons.append(f"{backend} sandbox is unknown: {sandbox}")
        elif backend in {"claude_code", "openbase_cloud"}:
            mode = values.get("SUPER_AGENTS_CLAUDE_PERMISSION_MODE", "bypassPermissions")
            normalized = mode.strip().lower()
            if normalized in UNSAFE_CLAUDE_PERMISSION_MODES:
                reasons.append(f"{backend} permission mode is unsafe: {mode}")
            elif normalized not in {"default", "acceptedits"}:
                reasons.append(f"{backend} permission mode is unknown: {mode}")
        else:
            reasons.append(f"configured backend is unknown: {backend}")
    return BaselineCheck(not reasons, tuple(reasons), configured)


def check_managed_mcp_registration(
    *,
    codex_config: Path = CODEX_HOME_DIR / "config.toml",
    claude_config: Path = OPENBASE_CLAUDE_JSON_PATH,
) -> ManagedControlsCheck:
    """Ensure managed MCP registrations use the required Openbase wrapper."""
    reasons: list[str] = []
    if codex_config.exists():
        try:
            payload = tomllib.loads(codex_config.read_text(encoding="utf-8"))
            entry = payload.get("mcp_servers", {}).get("super-agents", {})
        except (OSError, tomllib.TOMLDecodeError, AttributeError):
            entry = {}
        if entry and not _is_managed_entry(entry):
            reasons.append("Codex Super Agents MCP registration bypasses Openbase execution controls")
    if claude_config.exists():
        try:
            payload = json.loads(claude_config.read_text(encoding="utf-8"))
            entry = payload.get("mcpServers", {}).get("super-agents", {})
        except (OSError, json.JSONDecodeError, AttributeError):
            entry = {}
        if entry and not _is_managed_entry(entry):
            reasons.append("Claude Super Agents MCP registration bypasses Openbase execution controls")
    return ManagedControlsCheck(not reasons, tuple(reasons))


def _is_managed_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    command = Path(str(entry.get("command") or "")).name
    args = entry.get("args") or []
    return command == "openbase-coder" and isinstance(args, list) and "super-agents-mcp" in args
