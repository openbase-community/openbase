"""Keep agent-only artifact directories out of every repository via the global Git ignore.

``.triggers/`` holds agent-to-agent messages (the counterpart of ``.reports/``
for people) and must never be committed, so setup adds it to the user's global
Git ignore file. ``.reports/`` is deliberately not added: private workspaces
may version their reports. The developer-setup system summary reports any
entry this adds, because it diffs the global ignore before and after setup.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import click

GLOBAL_GIT_IGNORE_ENTRIES: tuple[str, ...] = (".triggers/",)


def global_git_ignore_path() -> Path:
    """Return Git's global ignore file (``core.excludesFile`` or Git's default)."""
    result = subprocess.run(
        ["git", "config", "--global", "--path", "--get", "core.excludesFile"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode not in (0, 1):
        raise OSError("could not read global Git ignore configuration")
    if result.stdout.strip():
        return Path(result.stdout.strip()).expanduser()
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "git/ignore"
    )


def ensure_global_git_ignore_entries(
    entries: tuple[str, ...] = GLOBAL_GIT_IGNORE_ENTRIES,
) -> tuple[Path, list[str]]:
    """Append the missing entries to the global Git ignore; return (path, added)."""
    path = global_git_ignore_path()
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    present = {line.strip() for line in existing}
    missing = [entry for entry in entries if entry not in present]
    if not missing:
        return path, []
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        if existing and not path.read_text(encoding="utf-8").endswith("\n"):
            handle.write("\n")
        handle.write("\n".join(missing) + "\n")
    return path, missing


def ensure_global_git_ignore() -> None:
    """Setup step: add the agent artifact ignores, warning instead of failing."""
    try:
        path, added = ensure_global_git_ignore_entries()
    except (OSError, subprocess.TimeoutExpired) as exc:
        click.echo(
            "⚠️ Could not update the global Git ignore "
            f"({exc}); add {', '.join(GLOBAL_GIT_IGNORE_ENTRIES)} to it yourself."
        )
        return
    if added:
        click.echo(f"Added {', '.join(added)} to the global Git ignore at {path}")
