"""First-class sync for git worktrees (code-sync layer 2 extension).

A linked worktree's files sync like any files, but its ``.git`` pointer is
machine-local (an absolute path into the main repo's ``.git/worktrees/``),
so on the peer a worktree would otherwise arrive as a plain folder with no
git identity. Reconciliation itself needs nothing new — a worktree's
checked-out branch is an ordinary shared-repo branch, and the git endpoint
serves refs through a worktree path — so the missing piece is exactly one
thing: materializing a matching worktree on the peer.

Origin side: every discovered worktree gets a small synced manifest
(``.openbase-worktree.json``) naming its main repo (home-relative) and
branch, kept out of ``git status`` via the worktree's machine-local
``info/exclude``. Peer side: a directory holding a manifest but no ``.git``
is adopted — the branch is fetched from the peer when absent locally, a
worktree is created against the corresponding main repo, and its ``.git``
pointer is placed around the already-synced files. From then on ``git``
works in the worktree on either machine and commits reconcile back through
the normal branch fast-forward path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

WORKTREE_MANIFEST_NAME = ".openbase-worktree.json"
MANIFEST_SCHEMA_VERSION = 1
GIT_TIMEOUT_SECONDS = 60


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
    )


def _git_env(
    args: list[str], cwd: Path, env: dict[str, str] | None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
        env=env,
    )


def worktree_main_repo(repo: Path) -> Path | None:
    """The main repository directory when ``repo`` is a linked worktree."""
    result = _git(["rev-parse", "--git-dir", "--git-common-dir"], repo)
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    if len(lines) < 2:
        return None
    git_dir = (repo / lines[0]).resolve()
    common_dir = (repo / lines[1]).resolve()
    if git_dir == common_dir:
        return None  # A main checkout, not a linked worktree.
    return common_dir.parent


def ensure_worktree_manifest(repo: Path, home: Path | None = None) -> bool:
    """Write/refresh the synced manifest for a linked worktree.

    Returns True when ``repo`` is a linked worktree (manifest ensured).
    """
    from openbase_coder_cli.code_sync.repositories import (
        replaced_tips_for_publish,
        repository_state,
    )

    home = home or Path.home()
    main_repo = worktree_main_repo(repo)
    if main_repo is None:
        return False
    state = repository_state(repo)
    if state is None:
        return True  # Detached worktrees stay machine-local.
    try:
        main_relhome = str(main_repo.relative_to(home))
    except ValueError:
        return True  # Main repo outside home: peer can never attach it.

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "main_repo": main_relhome,
        **state,
    }
    replaces = replaced_tips_for_publish(
        repo, state["branch"], state["head"], read_manifest(repo)
    )
    if replaces:
        manifest["replaces"] = replaces
    path = repo / WORKTREE_MANIFEST_NAME
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != rendered:
            path.write_text(rendered, encoding="utf-8")
    except OSError:
        return True
    _exclude_manifest(repo)
    return True


def _exclude_manifest(repo: Path) -> None:
    """Keep the manifest out of git status via the machine-local exclude."""
    result = _git(["rev-parse", "--git-path", "info/exclude"], repo)
    if result.returncode != 0:
        return
    exclude = (repo / result.stdout.strip()).resolve()
    line = f"/{WORKTREE_MANIFEST_NAME}"
    try:
        content = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if line not in content.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(
                (content.rstrip("\n") + "\n" if content else "") + line + "\n",
                encoding="utf-8",
            )
    except OSError:
        pass


def read_manifest(directory: Path) -> dict[str, Any] | None:
    path = directory / WORKTREE_MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if not payload.get("main_repo") or not payload.get("branch"):
        return None
    return payload


def adopt_worktree(
    directory: Path,
    *,
    home: Path | None = None,
    remote_url: str | None = None,
    auth_header: str | None = None,
) -> str:
    """Attach a real worktree around a synced worktree-shaped directory.

    Returns an action string: ``adopted``, or a skip/failure reason.
    """
    from openbase_coder_cli.code_sync.reconciler import _auth_env

    home = home or Path.home()
    manifest = read_manifest(directory)
    if manifest is None:
        return "manifest_invalid"
    if (directory / ".git").exists():
        return "already_worktree"

    main_repo = home / manifest["main_repo"]
    branch = manifest["branch"]
    if not (main_repo / ".git").exists():
        return "main_repo_missing"

    branch_exists = (
        _git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], main_repo
        ).stdout.strip()
        != ""
    )
    if not branch_exists:
        if not remote_url:
            return "branch_missing_no_peer"
        fetch = _git_env(
            ["fetch", "--quiet", remote_url, branch],
            main_repo,
            _auth_env(auth_header),
        )
        if fetch.returncode != 0:
            return "branch_fetch_failed"
        fetched = _git(
            ["rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}"], main_repo
        ).stdout.strip()
        if not fetched:
            return "branch_fetch_failed"
        if _git(["branch", branch, fetched], main_repo).returncode != 0:
            return "branch_create_failed"

    # A branch can only be checked out in one worktree of a repo.
    in_use = _git(["worktree", "list", "--porcelain"], main_repo).stdout
    if f"branch refs/heads/{branch}\n" in in_use:
        return "branch_checked_out_elsewhere"

    # Materialize the worktree metadata without touching the synced files:
    # create a no-checkout worktree at a scratch path, transplant its .git
    # pointer into the target directory, then let git repair the linkage.
    with tempfile.TemporaryDirectory(prefix="code-sync-worktree-") as tmp:
        scratch = Path(tmp) / "wt"
        added = _git(
            ["worktree", "add", "--no-checkout", str(scratch), branch], main_repo
        )
        if added.returncode != 0:
            return f"worktree_add_failed: {added.stderr.strip()[:120]}"
        try:
            shutil.move(str(scratch / ".git"), str(directory / ".git"))
        except OSError:
            _git(["worktree", "remove", "--force", str(scratch)], main_repo)
            return "git_pointer_move_failed"
    repaired = _git(["worktree", "repair", str(directory)], main_repo)
    if repaired.returncode != 0:
        return f"worktree_repair_failed: {repaired.stderr.strip()[:120]}"
    # The no-checkout worktree has an empty index and a HEAD on the branch;
    # reset the index to HEAD so status reflects synced files vs the branch.
    _git(["reset", "--quiet"], directory)
    _exclude_manifest(directory)
    return "adopted"


def prune_worktrees(repo: Path) -> None:
    """Drop stale worktree metadata after a synced deletion removed the dir."""
    _git(["worktree", "prune"], repo)
