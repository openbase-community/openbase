"""Remove identifiable legacy Openbase entries without replacing user defaults."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from pathlib import Path

import tomlkit

from openbase_coder_cli.paths import INJECT_SESSION_ID_HOOK_PATH


def write_if_changed(path: Path, content: str, *, backup: bool = False) -> None:
    previous = path.read_text(encoding="utf-8") if path.is_file() else None
    if previous == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and previous is not None:
        backup_path = path.with_name(path.name + ".before-openbase-profiles")
        if not backup_path.exists():
            backup_path.write_text(previous, encoding="utf-8")
            backup_path.chmod(0o600)
    # Preserve dotfile-manager symlinks and never expose partial files to TUIs.
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def is_openbase_mcp(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    command = str(entry.get("command", ""))
    args = entry.get("args", [])
    env = entry.get("env", {})
    return (
        ("super-agents-mcp" in command or "super-agents-mcp" in args)
        and isinstance(env, dict)
        and any(
            key in env
            for key in (
                "SUPER_AGENTS_CODEX_SANDBOX_POLICY",
                "SUPER_AGENTS_BASE_INSTRUCTIONS_PATH",
            )
        )
    )


def migrate_codex_user_config(path: Path) -> None:
    if not path.is_file():
        return
    existing = path.read_text(encoding="utf-8")
    document = tomlkit.parse(existing)
    servers = document.get("mcp_servers", {})
    if is_openbase_mcp(servers.get("super-agents")):
        del servers["super-agents"]
    # Removing a hook group shifts subsequent positional trust identities.
    # TOML permits hook tables interleaved with unrelated sections. Rebuild
    # just this subtree to avoid invalidating tomlkit's out-of-order proxies.
    hooks = tomllib.loads(existing).get("hooks", {})
    groups = hooks.get("SessionStart", [])
    command = str(INJECT_SESSION_ID_HOOK_PATH)
    removed = [
        index
        for index, group in enumerate(groups)
        if group.get("hooks")
        and all(hook.get("command") == command for hook in group["hooks"])
    ]
    if removed:
        state = hooks.get("state", {})
        prefix = f"{path.resolve()}:session_start:"
        renamed = dict(state.items())
        for key, value in state.items():
            if not key.startswith(prefix):
                continue
            index_text, _, suffix = key[len(prefix) :].partition(":")
            if not index_text.isdigit():
                continue
            index = int(index_text)
            del renamed[key]
            if index not in removed:
                renamed[
                    f"{prefix}{index - sum(old < index for old in removed)}:{suffix}"
                ] = value
        hooks["state"] = renamed
        for index in reversed(removed):
            del groups[index]
        document["hooks"] = hooks
    write_if_changed(path, tomlkit.dumps(document), backup=True)


def migrate_claude_user_config(state_path: Path, settings_path: Path) -> None:
    for path in (state_path, settings_path):
        if not path.is_file():
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"Expected JSON object in {path}")
        before = json.dumps(document, sort_keys=True)
        servers = document.get("mcpServers", {})
        if is_openbase_mcp(servers.get("super-agents")):
            del servers["super-agents"]
        hooks = document.get("hooks", {})
        groups = hooks.get("SessionStart", [])
        command = str(INJECT_SESSION_ID_HOOK_PATH)
        for group in list(groups):
            entries = group.get("hooks", [])
            kept = [hook for hook in entries if hook.get("command") != command]
            if kept == entries:
                continue
            if kept:
                group["hooks"] = kept
            else:
                groups.remove(group)
        if json.dumps(document, sort_keys=True) != before:
            write_if_changed(path, json.dumps(document, indent=2) + "\n", backup=True)
