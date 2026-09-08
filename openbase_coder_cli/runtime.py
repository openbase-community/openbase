from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from openbase_coder_cli.paths import STANDALONE_CURRENT_DIR

PACKAGE_METADATA_FILENAME = "openbase-coder-package.json"


@dataclass(frozen=True)
class RuntimePackage:
    root: Path
    version: str = ""
    target: str = ""
    channel: str = "stable"

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def python_path(self) -> Path:
        if os.name == "nt":
            return self.root / "python" / "python.exe"
        return self.root / "python" / "bin" / "python"

    @property
    def openbase_coder_path(self) -> Path:
        name = "openbase-coder.exe" if os.name == "nt" else "openbase-coder"
        return self.bin_dir / name

    @property
    def livekit_server_path(self) -> Path:
        name = "livekit-server.exe" if os.name == "nt" else "livekit-server"
        return self.bin_dir / name

    @property
    def console_build_dir(self) -> Path:
        return self.root / "console"

    @property
    def instructions_dir(self) -> Path:
        return self.root / "instructions"

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"


def current_runtime_package() -> RuntimePackage | None:
    """Return the standalone package root when running from one."""
    explicit_root = os.environ.get("OPENBASE_CODER_PACKAGE_DIR", "").strip()
    candidates: list[Path] = []
    if explicit_root:
        candidates.append(Path(explicit_root))

    candidates.extend(_candidate_roots_from_executable())

    for candidate in candidates:
        package = _package_from_root(candidate)
        if package is not None:
            return package
    return None


def packaged_console_build_dir() -> Path | None:
    package = current_runtime_package()
    if package is None:
        return None
    return package.console_build_dir if package.console_build_dir.is_dir() else None


def packaged_instructions_dir() -> Path | None:
    package = current_runtime_package()
    if package is None:
        return None
    return package.instructions_dir if package.instructions_dir.is_dir() else None


def packaged_skills_dir() -> Path | None:
    package = current_runtime_package()
    if package is None:
        return None
    return package.skills_dir if package.skills_dir.is_dir() else None


def is_standalone_runtime() -> bool:
    return current_runtime_package() is not None


def stable_runtime_package() -> RuntimePackage | None:
    """current_runtime_package() with its root routed through ``current``.

    Anything that persists a package path (service wrappers, plists) must use
    this instead of the raw detection: detection can land on the versioned
    release directory (which is pruned on updates) depending on how the
    process was launched, while the ``packages/standalone/current`` alias
    survives every flip.
    """
    package = current_runtime_package()
    if package is None:
        return None
    stable_root = stable_package_path(package.root)
    if stable_root == package.root:
        return package
    return RuntimePackage(
        root=stable_root,
        version=package.version,
        target=package.target,
        channel=package.channel,
    )


def stable_package_path(path: Path) -> Path:
    """Return the version-independent alias for a standalone package path.

    Release directories rotate on every self-update and the old version is
    deleted, so persisted references — MCP command paths, skill symlinks —
    must route through the stable ``packages/standalone/current`` symlink or
    they dangle after the next release (and break when synced to a machine on
    a different release). Paths outside the release that ``current`` points
    to are returned unchanged.
    """
    try:
        current_target = STANDALONE_CURRENT_DIR.resolve(strict=True)
    except OSError:
        return path
    try:
        relative = path.resolve().relative_to(current_target)
    except (OSError, ValueError):
        return path
    return STANDALONE_CURRENT_DIR / relative


def _candidate_roots_from_executable() -> list[Path]:
    executable = Path(sys.executable).resolve()
    candidates: list[Path] = []
    for parent in [executable.parent, *executable.parents]:
        candidates.append(parent)
    return candidates


def _package_from_root(root: Path) -> RuntimePackage | None:
    metadata_path = root / PACKAGE_METADATA_FILENAME
    if not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = {}
    return RuntimePackage(
        root=root,
        version=str(metadata.get("version", "")),
        target=str(metadata.get("target", "")),
        channel=str(metadata.get("channel", "")).strip() or "stable",
    )
