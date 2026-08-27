"""Synced manifests for ordinary git repositories.

The file layer cannot safely copy ``.git``, so each main checkout publishes a
small manifest containing the checked-out branch and commit.  A peer uses the
manifest to create a missing repository or converge an existing checkout
without rewriting working-tree files (Syncthing owns those).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

REPO_MANIFEST_NAME = ".openbase-repo.json"
MANIFEST_SCHEMA_VERSION = 1
RECOVERY_REF_PREFIX = "refs/openbase-code-sync/backups"


def is_repository_manifest_conflict(path: Path) -> bool:
    """Whether ``path`` is a Syncthing conflict copy of our repo manifest."""
    return (
        path.name.startswith(".openbase-repo.sync-conflict-") and path.suffix == ".json"
    )


def discover_sync_checkouts(
    folder_root: Path, *, max_depth: int, skip_dir_names: set[str]
) -> tuple[list[Path], list[Path], list[Path]]:
    """Find attached repos plus manifest-only worktree/repo directories."""
    from openbase_coder_cli.code_sync.worktrees import WORKTREE_MANIFEST_NAME

    repos: list[Path] = []
    worktree_candidates: list[Path] = []
    repo_candidates: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        try:
            is_repo = (directory / ".git").exists()
        except OSError:
            return
        if is_repo:
            repos.append(directory)
            # Multi workspaces contain nested repositories, so keep walking.
        elif (directory / WORKTREE_MANIFEST_NAME).is_file():
            worktree_candidates.append(directory)
        elif (directory / REPO_MANIFEST_NAME).is_file():
            repo_candidates.append(directory)
        if depth >= max_depth:
            return
        try:
            children = sorted(directory.iterdir())
        except OSError:
            return
        for child in children:
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name.startswith(".") or child.name in skip_dir_names:
                continue
            walk(child, depth + 1)

    if folder_root.is_dir():
        walk(folder_root, 0)
    return repos, worktree_candidates, repo_candidates


def repository_state(repo: Path) -> dict[str, str] | None:
    """Return the current branch/head pair, or ``None`` when unavailable."""
    from openbase_coder_cli.code_sync.reconciler import _git, current_branch

    branch = current_branch(repo)
    if branch is None:
        return None
    head = _git(["rev-parse", "--verify", "HEAD^{commit}"], repo).stdout.strip()
    if not head:
        return None
    return {"branch": branch, "head": head}


def ensure_repository_manifest(repo: Path) -> dict[str, Any] | None:
    """Write or refresh this main checkout's synced repository manifest."""
    from openbase_coder_cli.code_sync.reconciler import _git

    state = repository_state(repo)
    if state is None:
        return None
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        **state,
    }
    origin = _git(["remote", "get-url", "origin"], repo).stdout.strip()
    if _safe_origin_url(origin):
        manifest["origin_url"] = origin
    path = repo / REPO_MANIFEST_NAME
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != rendered:
            path.write_text(rendered, encoding="utf-8")
    except OSError:
        return None
    _exclude_manifest(repo)
    return manifest


def read_repository_manifest(directory: Path) -> dict[str, Any] | None:
    """Read and validate a repository manifest."""
    try:
        payload = json.loads(
            (directory / REPO_MANIFEST_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        return None
    branch = str(payload.get("branch") or "")
    head = str(payload.get("head") or "")
    if (
        not branch
        or len(head) != 40
        or any(char not in "0123456789abcdef" for char in head)
    ):
        return None
    return payload


def sync_checkout_manifest(
    repo: Path,
    *,
    is_worktree: bool,
    home: Path | None,
    previous_state: dict[str, str] | None,
    remote_urls: Iterable[str],
    auth_header: str | None,
    folder_id: str = "",
    repo_relpath: str = "",
    conflicts_path: Path | None = None,
) -> str:
    """Publish a local checkout change or consume its synced manifest."""
    from openbase_coder_cli.code_sync.worktrees import (
        ensure_worktree_manifest,
    )
    from openbase_coder_cli.code_sync.worktrees import (
        read_manifest as read_worktree_manifest,
    )

    current_state = repository_state(repo)
    manifest = (
        read_worktree_manifest(repo) if is_worktree else read_repository_manifest(repo)
    )

    def publish() -> dict[str, Any] | None:
        if is_worktree:
            ensure_worktree_manifest(repo, home)
            return read_worktree_manifest(repo)
        return ensure_repository_manifest(repo)

    if manifest is None or not manifest.get("head"):
        publish()
        return "published"
    if current_state is not None and any(
        manifest.get(key) != value for key, value in current_state.items()
    ):
        locally_changed = previous_state is not None and current_state != previous_state
        if locally_changed:
            publish()
            return "published_local_change"
        action = converge_repository_to_manifest(
            repo,
            manifest,
            remote_urls=remote_urls,
            auth_header=auth_header,
            folder_id=folder_id,
            repo_relpath=repo_relpath,
            conflicts_path=conflicts_path,
        )
        if action == "local_ahead":
            # The peer's manifest lags this machine; local history wins and
            # the peers converge to US on their next tick.
            publish()
            return "local_ahead_republished"
        return action
    if not is_worktree:
        # Refresh a newly added/changed safe origin URL even when the branch
        # state itself did not move.
        ensure_repository_manifest(repo)
    return "unchanged"


def adopt_repository(
    directory: Path,
    *,
    remote_urls: Iterable[str],
    auth_header: str | None = None,
) -> str:
    """Create machine-local git metadata around already-synced files."""
    from openbase_coder_cli.code_sync.reconciler import _git

    manifest = read_repository_manifest(directory)
    if manifest is None:
        return "manifest_invalid"
    if (directory / ".git").exists():
        return "already_repository"
    branch = str(manifest["branch"])
    if not _valid_branch(directory, branch):
        return "branch_invalid"
    initialized = _git(["init", "-q", "-b", branch], directory)
    if initialized.returncode != 0:
        return f"init_failed: {initialized.stderr.strip()[:120]}"
    origin = str(manifest.get("origin_url") or "")
    if _safe_origin_url(origin):
        _git(["remote", "add", "origin", origin], directory)
    fetched = _fetch_manifest_head(
        directory, manifest, remote_urls=remote_urls, auth_header=auth_header
    )
    if fetched != "ready":
        return fetched
    head = str(manifest["head"])
    update = _git(["update-ref", f"refs/heads/{branch}", head], directory)
    if update.returncode != 0:
        return "head_update_failed"
    _git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], directory)
    reset = _git(["reset", "--mixed", "--quiet"], directory)
    if reset.returncode != 0:
        return "index_refresh_failed"
    _exclude_manifest(directory)
    return "adopted"


def converge_repository_to_manifest(
    repo: Path,
    manifest: dict[str, Any],
    *,
    remote_urls: Iterable[str],
    auth_header: str | None = None,
    folder_id: str = "",
    repo_relpath: str = "",
    conflicts_path: Path | None = None,
) -> str:
    """Converge branch/head to the manifest while preserving file content.

    Convergence only performs provably-safe moves. A manifest whose head is
    an ANCESTOR of the local head is stale (the peer lags this machine) —
    the local state wins and the caller republishes it. Truly divergent
    history PAUSES: the ref stays put, a branch conflict is recorded for
    health warnings and ``openbase-coder sync resolve``, and no winner is
    picked automatically (the 2026-08-25 incident). Setting
    ``OPENBASE_CODE_SYNC_AUTO_DISPLACE=1`` restores the legacy behavior of
    displacing to the manifest with the old tip retained under
    ``refs/openbase-code-sync/backups``.
    """
    from openbase_coder_cli.code_sync.reconciler import (
        _git,
        current_branch,
        operation_in_progress,
    )

    if operation_in_progress(repo):
        return "operation_in_progress"
    current = current_branch(repo)
    if current is None:
        return "detached_head"
    if _git(["diff", "--cached", "--quiet"], repo).returncode != 0:
        return "staged_changes"
    branch = str(manifest.get("branch") or "")
    if not _valid_branch(repo, branch):
        return "branch_invalid"
    fetched = _fetch_manifest_head(
        repo, manifest, remote_urls=remote_urls, auth_header=auth_header
    )
    if fetched != "ready":
        return fetched
    desired_head = str(manifest["head"])

    if current != branch and _branch_checked_out_elsewhere(repo, branch):
        return "branch_checked_out_elsewhere"

    target_ref = f"refs/heads/{branch}"
    old_target = _git(
        ["rev-parse", "--verify", "--quiet", f"{target_ref}^{{commit}}"], repo
    ).stdout.strip()
    backup = ""
    if old_target and old_target != desired_head:
        is_fast_forward = (
            _git(
                ["merge-base", "--is-ancestor", old_target, desired_head], repo
            ).returncode
            == 0
        )
        if not is_fast_forward:
            manifest_is_stale = (
                _git(
                    ["merge-base", "--is-ancestor", desired_head, old_target], repo
                ).returncode
                == 0
            )
            if manifest_is_stale:
                return "local_ahead"
            from openbase_coder_cli.code_sync.conflicts import (
                record_branch_conflict,
            )

            record_branch_conflict(
                folder_id=folder_id,
                repo_relpath=repo_relpath,
                branch=branch,
                local_sha=old_target,
                remote_sha=desired_head,
                path=conflicts_path,
            )
            if os.environ.get("OPENBASE_CODE_SYNC_AUTO_DISPLACE") != "1":
                # Truly divergent history: neither machine's automation has
                # standing to pick a winner. The ref stays put (Syncthing has
                # already made the files match, so the machines are not
                # materially out of step), the conflict surfaces in health
                # warnings, and `openbase-coder sync resolve` decides.
                return "divergence_paused"
            # Legacy opt-in: displace to the manifest, preserving the old tip.
            backup = _preserve_commit(repo, old_target)
    update_args = ["update-ref", target_ref, desired_head]
    if old_target:
        update_args.append(old_target)
    update = _git(update_args, repo)
    if update.returncode != 0:
        return "head_update_failed"

    if current != branch:
        switched = _git(["symbolic-ref", "HEAD", target_ref], repo)
        if switched.returncode != 0:
            return "branch_switch_failed"
    reset = _git(["reset", "--mixed", "--quiet"], repo)
    if reset.returncode != 0:
        return "index_refresh_failed"
    _exclude_manifest(repo)
    action = "converged"
    return f"{action}; backup={backup}" if backup else action


def _fetch_manifest_head(
    repo: Path,
    manifest: dict[str, Any],
    *,
    remote_urls: Iterable[str],
    auth_header: str | None,
) -> str:
    from openbase_coder_cli.code_sync.reconciler import _auth_env, _git

    branch = str(manifest["branch"])
    desired_head = str(manifest["head"])
    if _git(["cat-file", "-e", f"{desired_head}^{{commit}}"], repo).returncode == 0:
        return "ready"
    last_error = ""
    for remote_url in remote_urls:
        result = _git(
            ["fetch", "--quiet", remote_url, branch],
            repo,
            env=_auth_env(auth_header),
        )
        if result.returncode != 0:
            last_error = result.stderr.strip()[:120]
            continue
        if _git(["cat-file", "-e", f"{desired_head}^{{commit}}"], repo).returncode == 0:
            return "ready"
    return f"fetch_failed: {last_error}" if last_error else "manifest_head_unavailable"


def _preserve_commit(repo: Path, commit_sha: str) -> str:
    from openbase_coder_cli.code_sync.reconciler import _git

    suffix = f"{int(time.time())}-{commit_sha[:12]}"
    recovery_ref = f"{RECOVERY_REF_PREFIX}/{suffix}"
    counter = 1
    while _git(["show-ref", "--verify", "--quiet", recovery_ref], repo).returncode == 0:
        counter += 1
        recovery_ref = f"{RECOVERY_REF_PREFIX}/{suffix}-{counter}"
    update = _git(["update-ref", recovery_ref, commit_sha], repo)
    return recovery_ref if update.returncode == 0 else ""


def _branch_checked_out_elsewhere(repo: Path, branch: str) -> bool:
    from openbase_coder_cli.code_sync.reconciler import _git

    blocks = _git(["worktree", "list", "--porcelain"], repo).stdout.split("\n\n")
    repo_path = repo.resolve()
    target = f"branch refs/heads/{branch}"
    for block in blocks:
        lines = block.splitlines()
        if target not in lines:
            continue
        path_line = next((line for line in lines if line.startswith("worktree ")), "")
        if (
            path_line
            and Path(path_line.removeprefix("worktree ")).resolve() != repo_path
        ):
            return True
    return False


def _exclude_manifest(repo: Path) -> None:
    from openbase_coder_cli.code_sync.reconciler import _git

    result = _git(["rev-parse", "--git-path", "info/exclude"], repo)
    if result.returncode != 0:
        return
    exclude = (repo / result.stdout.strip()).resolve()
    line = f"/{REPO_MANIFEST_NAME}"
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


def _valid_branch(repo: Path, branch: str) -> bool:
    from openbase_coder_cli.code_sync.reconciler import _git

    return _git(["check-ref-format", "--branch", branch], repo).returncode == 0


def _safe_origin_url(url: str) -> bool:
    """Allow portable remotes, but never serialize embedded HTTP credentials."""
    if not url or os.path.isabs(url) or url.startswith(("./", "../", "~")):
        return False
    if "://" in url:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http", "ssh", "git"}:
            return False
        if parsed.password or (parsed.scheme in {"https", "http"} and parsed.username):
            return False
        return bool(parsed.hostname)
    return "@" in url and ":" in url
