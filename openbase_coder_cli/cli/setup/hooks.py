"""Session-ID hooks: SessionStart hooks for both Openbase agent homes.

Installs the bundled ``inject-session-id.sh`` script into ``~/.openbase/hooks``
and registers it as a SessionStart hook in the Openbase Claude settings and
the Openbase Codex home config. The hook feeds each session's thread/session
ID back into the conversation as additionalContext so agents can stamp
commits with the ``Agent-Thread-Id`` trailer.

Codex only runs hooks whose ``[hooks.state]`` entry pins a ``trusted_hash``
matching the hook's normalized identity, so this module replicates codex's
fingerprint (``codex-rs/config/src/fingerprint.rs``): sha256 over the compact,
key-sorted JSON form of the TOML-normalized hook identity.
"""

from __future__ import annotations

import hashlib
import importlib.resources as importlib_resources
import json
from pathlib import Path

import click

from openbase_coder_cli.paths import (
    CODEX_HOME_DIR,
    INJECT_SESSION_ID_HOOK_PATH,
    OPENBASE_HOOKS_DIR,
)

BUNDLED_HOOKS_PACKAGE = "openbase_coder_cli.resources.hooks"
SESSION_ID_HOOK_FILENAME = "inject-session-id.sh"
# Codex normalizes an absent hook timeout to 600s before hashing, so the
# timeout is part of the trust identity even though we never write it.
_CODEX_HOOK_DEFAULT_TIMEOUT_SEC = 600


def ensure_session_id_hook_script() -> None:
    """Install the bundled session-ID hook script into ~/.openbase/hooks."""
    OPENBASE_HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    resource = importlib_resources.files(BUNDLED_HOOKS_PACKAGE).joinpath(
        SESSION_ID_HOOK_FILENAME
    )
    content = resource.read_text(encoding="utf-8")
    if (
        not INJECT_SESSION_ID_HOOK_PATH.is_file()
        or INJECT_SESSION_ID_HOOK_PATH.read_text(encoding="utf-8") != content
    ):
        INJECT_SESSION_ID_HOOK_PATH.write_text(content, encoding="utf-8")
        click.echo(f"Installed session-ID hook script at {INJECT_SESSION_ID_HOOK_PATH}")
    INJECT_SESSION_ID_HOOK_PATH.chmod(0o755)


def merge_session_id_hook_into_claude_hooks(value: object) -> dict[str, object]:
    """Return Claude settings ``hooks`` with the SessionStart session-ID hook."""
    hooks = dict(value) if isinstance(value, dict) else {}
    session_start = hooks.get("SessionStart")
    groups = list(session_start) if isinstance(session_start, list) else []
    command = str(INJECT_SESSION_ID_HOOK_PATH)
    if not any(_claude_group_has_command(group, command) for group in groups):
        groups.append(
            {"matcher": "", "hooks": [{"type": "command", "command": command}]}
        )
    hooks["SessionStart"] = groups
    return hooks


def ensure_claude_session_id_hook(settings_path: Path) -> bool:
    """Register the session-ID hook in one Claude settings file."""
    if settings_path.is_file():
        try:
            value = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise click.ClickException(
                f"Could not read Claude settings at {settings_path}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise click.ClickException(
                f"Claude settings must contain a JSON object: {settings_path}"
            )
        existing = value
    else:
        existing = {}

    updated = {
        **existing,
        "hooks": merge_session_id_hook_into_claude_hooks(existing.get("hooks")),
    }
    if updated == existing:
        click.echo(f"Claude session-ID hook already configured in {settings_path}")
        return False

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    settings_path.chmod(0o600)
    click.echo(f"Configured Claude session-ID hook in {settings_path}")
    return True


def _claude_group_has_command(group: object, command: str) -> bool:
    if not isinstance(group, dict):
        return False
    entries = group.get("hooks")
    if not isinstance(entries, list):
        return False
    return any(
        isinstance(entry, dict) and entry.get("command") == command for entry in entries
    )


def session_start_hook_trusted_hash(command: str) -> str:
    """Compute codex's trust hash for a bare SessionStart command hook."""
    identity = {
        "event_name": "session_start",
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": _CODEX_HOOK_DEFAULT_TIMEOUT_SEC,
                "async": False,
            }
        ],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def ensure_codex_session_id_hook(config_path: Path | None = None) -> bool:
    """Register the session-ID hook and its trust state in the codex config.

    Returns True when the config file changed.
    """
    path = config_path or CODEX_HOME_DIR / "config.toml"
    command = str(INJECT_SESSION_ID_HOOK_PATH)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    existing_groups = _session_start_hook_commands(existing)
    try:
        group_index = next(
            index for index, commands in enumerate(existing_groups) if command in commands
        )
        hook_is_present = True
    except StopIteration:
        group_index = len(existing_groups)
        hook_is_present = False

    # Codex keys hook state by the canonicalized source path plus the hook's
    # position within it. Preserve user-defined groups and trust our actual
    # array index rather than assuming the managed hook is first.
    resolved_path = path.parent.resolve() / path.name
    state_key = f"{resolved_path}:session_start:{group_index}:0"

    hook_lines = [
        "[[hooks.SessionStart]]",
        "",
        "[[hooks.SessionStart.hooks]]",
        'type = "command"',
        f"command = {json.dumps(command)}",
    ]
    state_lines = [
        f'[hooks.state."{state_key}"]',
        f"trusted_hash = {json.dumps(session_start_hook_trusted_hash(command))}",
        "enabled = true",
    ]

    trusted_hash = session_start_hook_trusted_hash(command)
    stripped = _remove_toml_sections(
        existing,
        {
            state_lines[0],
            *_state_headers_with_trusted_hash(existing, trusted_hash),
        },
    )
    blocks = [stripped.rstrip("\n")] if stripped.strip() else []
    if not hook_is_present:
        blocks.append("\n".join(hook_lines))
    blocks.append("\n".join(state_lines))
    updated = "\n\n".join(blocks) + "\n"
    if updated == existing:
        click.echo(f"Codex session-ID hook already configured in {path}")
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    click.echo(f"Configured codex session-ID hook in {path}")
    return True


def _session_start_hook_commands(text: str) -> list[list[str]]:
    groups: list[list[str]] = []
    active_group: list[str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[[hooks.SessionStart]]":
            active_group = []
            groups.append(active_group)
            continue
        if (
            stripped.startswith("[")
            and stripped.endswith("]")
            and stripped != "[[hooks.SessionStart.hooks]]"
        ):
            active_group = None
            continue
        if active_group is None or not stripped.startswith("command ="):
            continue
        raw_value = stripped.partition("=")[2].strip()
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            continue
        if isinstance(value, str):
            active_group.append(value)
    return groups


def _state_headers_with_trusted_hash(text: str, trusted_hash: str) -> set[str]:
    lines = text.splitlines()
    matches: set[str] = set()
    index = 0
    while index < len(lines):
        header = lines[index].strip()
        if not header.startswith('[hooks.state."'):
            index += 1
            continue
        section_end = index + 1
        while section_end < len(lines):
            candidate = lines[section_end].strip()
            if candidate.startswith("[") and candidate.endswith("]"):
                break
            section_end += 1
        expected_line = f"trusted_hash = {json.dumps(trusted_hash)}"
        if any(lines[offset].strip() == expected_line for offset in range(index + 1, section_end)):
            matches.add(header)
        index = section_end
    return matches


def _remove_toml_sections(text: str, headers: set[str]) -> str:
    """Drop every section whose header line is in ``headers``.

    A section runs from its header line to the next ``[...]`` header. Other
    sections — including other ``[hooks.state."..."]`` entries — are kept.
    """
    lines = text.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() in headers:
            index += 1
            while index < len(lines):
                stripped = lines[index].strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    break
                index += 1
            while output and not output[-1].strip():
                output.pop()
            continue
        output.append(lines[index])
        index += 1
    return "\n".join(output) + ("\n" if output else "")
