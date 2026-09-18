"""Small, read-only source/artifact probes. No release-feed or network access."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

SCHEMA_VERSION = 1


def revision(repo: Path) -> str | None:
    """Require an actual repository root (including linked Git worktrees)."""
    if not (repo / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None  # Unavailable evidence is unknown, never current.
    value = result.stdout.strip()
    return value if result.returncode == 0 and len(value) == 40 else None


def workspace_id(workspace: Path) -> str:
    # Opaque host-local identity; never send filesystem paths to a renderer.
    return hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()


def file_digest(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return None


def read_manifest(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return None
    return value


def write_manifest(path: Path, value: dict) -> None:
    """Private, atomic record; a failed capture must not prevent service startup."""
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".freshness-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
