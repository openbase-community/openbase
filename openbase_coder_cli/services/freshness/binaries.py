"""Compare external engines with source pins without importing a stale constant."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path


def livekit_matches_pin(binary: Path, workspace: Path) -> bool | None:
    try:
        tree = ast.parse(
            (workspace / "cli/openbase_coder_cli/livekit_version.py").read_text()
        )
        expected = next(
            node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "LIVEKIT_SERVER_PINNED_VERSION"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
        )
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, SyntaxError, StopIteration, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\b\d+\.\d+\.\d+\b", result.stdout + result.stderr)
    if result.returncode or not match:
        return None
    return match.group() == expected
