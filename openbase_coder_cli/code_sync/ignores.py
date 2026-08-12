"""Generated ``.stignore`` files for code-sync folders.

openbase-coder owns the whole file (per-folder overrides live in
``sync-config.json`` as ``extra_ignores``, not in hand edits). The critical
invariant is that VCS metadata never syncs: ``.git`` is a multi-file database
mutated non-atomically, and syncing it tears refs/index state on the peer.

Deliberately NOT ignored: ``.env`` files and other gitignored secrets inside
project directories — syncing them between the user's machines is a feature
of code sync (git transports alone cannot move them).
"""

from __future__ import annotations

from pathlib import Path

from openbase_coder_cli.sync_config import SyncFolder, sync_folders

STIGNORE_FILENAME = ".stignore"
MANAGED_HEADER = (
    "// Managed by openbase-coder code-sync — do not edit; this file is\n"
    "// regenerated whenever sync settings change.\n"
    "// Note: .env files and gitignored secrets are deliberately NOT ignored;\n"
    "// syncing them between your machines is a feature of code sync.\n"
)
# Bare names match at every level including the folder root (Syncthing
# expands `name` to `name`, `**/name`, `name/**`, `**/name/**`); `**/name`
# alone misses the root. `(?d)` marks entries deletable so an ignored child
# never blocks propagating its parent directory's deletion.
VCS_PATTERNS = (
    "// VCS metadata must never sync (torn git state)",
    "(?d).git",
    "(?d)**/.git",  # redundant with the bare pattern; kept as insurance
    "(?d).jj",
    "(?d).hg",
)
DEPENDENCY_PATTERNS = (
    "// Dependencies and virtualenvs",
    "(?d)node_modules",
    "(?d).venv",
    "(?d)venv",
)
# Dependency lockfiles regenerate independently on each machine and are
# transported by git (they are committed), so syncing them adds nothing but
# `.sync-conflict-*` churn when two machines resolve/regenerate in parallel.
# This mirrors the .env carve-out logic in reverse: git already moves these,
# so code sync should stay out of the way.
LOCKFILE_PATTERNS = (
    "// Dependency lockfiles (git already transports these; syncing only",
    "// produces conflict copies when both machines regenerate them)",
    "(?d)uv.lock",
    "(?d)package-lock.json",
    "(?d)pnpm-lock.yaml",
    "(?d)yarn.lock",
    "(?d)bun.lockb",
    "(?d)Cargo.lock",
    "(?d)poetry.lock",
    "(?d)Gemfile.lock",
    "(?d)composer.lock",
    "(?d)Podfile.lock",
)
BUILD_PATTERNS = (
    "// Build outputs",
    "(?d)dist",
    "(?d)build",
    "(?d)out",
    "(?d)release",
    "(?d)DerivedData",
    "(?d)__pycache__",
    "(?d).next",
    "(?d)target",
)
CACHE_PATTERNS = (
    "// Caches and local databases",
    "(?d).DS_Store",
    "(?d)*.sqlite3",
    "(?d).pytest_cache",
    "(?d).ruff_cache",
    "(?d).mypy_cache",
    "(?d).terraform",
    # A user-managed Syncthing writes version copies inside the folder;
    # those must never ride along to peers.
    "(?d).stversions",
)


def render_stignore(extra_ignores: tuple[str, ...] = ()) -> str:
    sections = [
        MANAGED_HEADER,
        "\n".join(VCS_PATTERNS),
        "\n".join(DEPENDENCY_PATTERNS),
        "\n".join(LOCKFILE_PATTERNS),
        "\n".join(BUILD_PATTERNS),
        "\n".join(CACHE_PATTERNS),
    ]
    if extra_ignores:
        sections.append("\n".join(("// Folder-specific ignores", *extra_ignores)))
    return "\n".join(sections) + "\n"


def write_folder_ignores(folder: SyncFolder, home: Path | None = None) -> Path:
    """Write the managed .stignore at the folder root (creating the folder)."""
    folder_root = folder.absolute_path(home)
    folder_root.mkdir(parents=True, exist_ok=True)
    stignore_path = folder_root / STIGNORE_FILENAME
    stignore_path.write_text(render_stignore(folder.extra_ignores), encoding="utf-8")
    return stignore_path


def update_all_ignores(
    config_path: Path | None = None, home: Path | None = None
) -> list[Path]:
    return [write_folder_ignores(folder, home) for folder in sync_folders(config_path)]
