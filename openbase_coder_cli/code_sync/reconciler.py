"""Git-state reconciler for code-sync (layer 2).

Syncthing moves working-tree files; this module keeps git branch pointers in
step through git's own transport. For each git repo inside a synced folder
it fetches the current branch from each peer's smart-HTTP endpoint (served
by the peer's Django CLI server over Tailscale) and fast-forwards the local
branch ONLY when it is provably safe:

- no merge/rebase in progress in the repo,
- the local head is an ancestor of the fetched head, and
- the local working tree already matches the fetched commit's tree
  (Syncthing has delivered the files, so nothing moves twice).

Divergence is normally converged through a synced repository manifest while
preserving displaced commits under recovery refs. Unsafe states remain
untouched and surface as conflict records for the product UI.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from openbase_coder_cli.code_sync.conflicts import (
    mark_branch_conflicts_resolved,
    reconcile_file_conflicts_against_disk,
    record_branch_conflict,
    record_file_conflict,
    unresolved_conflicts,
)
from openbase_coder_cli.code_sync.eligibility import (
    SyncPeer,
    current_eligibility,
    syncable_peers,
)
from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
    TokenManager,
)
from openbase_coder_cli.paths import CODE_SYNC_DIR
from openbase_coder_cli.services.onboarding import web_backend_url
from openbase_coder_cli.sync_config import (
    SyncFolder,
    code_sync_enabled,
    sync_folders,
)

MAX_REPO_DEPTH = 5
PEER_API_PORT = 18080  # Django CLI server exposed on the tailnet.
GIT_TIMEOUT_SECONDS = 60
RECONCILE_STATE_PATH = CODE_SYNC_DIR / "reconcile-state.json"
SKIP_DIR_NAMES = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "DerivedData",
    ".local",
    "companion-build",
    "dist",
    "build",
    "out",
    "release",
    "target",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".terraform",
    # Version copies from a (legacy) in-folder Syncthing versioner are
    # archives, not live conflicts.
    ".stversions",
}
SKIP_FILE_CONFLICT_PATH_SEGMENTS = (
    ("logs", "launchd"),
    ("data", "db"),
)

ACTION_FAST_FORWARDED = "fast_forwarded"
ACTION_UP_TO_DATE = "up_to_date"
ACTION_AWAITING_FILES = "awaiting_files"
ACTION_REMOTE_BEHIND = "remote_behind"
ACTION_DIVERGED = "diverged"
ACTION_SKIPPED_IN_PROGRESS = "skipped_in_progress"
ACTION_SKIPPED_DETACHED = "skipped_detached"
ACTION_FETCH_FAILED = "fetch_failed"


@dataclass(frozen=True)
class RepoReconcileResult:
    folder_id: str
    repo_relpath: str
    branch: str
    action: str
    detail: str = ""


def discover_git_repos(
    folder_root: Path, max_depth: int = MAX_REPO_DEPTH
) -> list[Path]:
    """Directories containing ``.git`` within ``max_depth`` of the root."""
    return discover_repos_and_worktree_candidates(folder_root, max_depth)[0]


def discover_repos_and_worktree_candidates(
    folder_root: Path, max_depth: int = MAX_REPO_DEPTH
) -> tuple[list[Path], list[Path]]:
    """One walk yielding repos and synced-but-unattached worktree dirs.

    A worktree candidate holds a worktree manifest (synced from the peer)
    but no ``.git`` — its git identity is machine-local and must be
    materialized here by adoption.
    """
    repos, worktree_candidates, _repo_candidates = discover_repos_and_candidates(
        folder_root, max_depth
    )
    return repos, worktree_candidates


def discover_repos_and_candidates(
    folder_root: Path, max_depth: int = MAX_REPO_DEPTH
) -> tuple[list[Path], list[Path], list[Path]]:
    """One walk yielding repos plus unattached worktree/repo directories."""
    from openbase_coder_cli.code_sync.repositories import discover_sync_checkouts

    return discover_sync_checkouts(
        folder_root, max_depth=max_depth, skip_dir_names=SKIP_DIR_NAMES
    )


def peer_git_url(peer: SyncPeer, folder_id: str, repo_relpath: str) -> str:
    host = peer.tailscale_magic_dns.rstrip(".")
    base = f"http://{host}:{PEER_API_PORT}/api/sync/git/{folder_id}"
    if repo_relpath:
        return f"{base}/{quote(repo_relpath)}"
    return base


def _git(
    args: list[str], cwd: Path, env: dict[str, str] | None = None
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


def _auth_env(auth_header: str | None) -> dict[str, str] | None:
    """git config via environment so the token never appears in ``ps``."""
    if not auth_header:
        return None
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: {auth_header}",
    }


def current_branch(repo: Path) -> str | None:
    result = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo)
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch else None


def operation_in_progress(repo: Path) -> bool:
    git_dir_result = _git(["rev-parse", "--git-dir"], repo)
    if git_dir_result.returncode != 0:
        return True  # Unreadable repo: never touch it.
    git_dir = (repo / git_dir_result.stdout.strip()).resolve()
    return any(
        (git_dir / marker).exists()
        for marker in (
            "MERGE_HEAD",
            "rebase-merge",
            "rebase-apply",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "BISECT_LOG",
        )
    )


def worktree_matches_commit(repo: Path, commit_sha: str) -> bool:
    """Whether the working tree content equals ``commit_sha``'s tree.

    ``git diff <commit>`` alone reports paths the commit adds but the local
    index does not know as deletions — even when Syncthing has already
    delivered identical content as untracked files. Build a throwaway index
    seeded from the commit and update it from the worktree instead, then
    compare tree hashes. ``add -u`` updates only the seeded (= committed)
    paths, so extra untracked files — in-flight uncommitted work, which is
    the normal state of a live machine — never block a fast-forward, and
    neither do untracked-and-gitignored files like ``.env``. Files the
    commit tracks but the local repo gitignores are still compared (they
    are tracked in the throwaway index).
    """
    target_tree = _git(["rev-parse", f"{commit_sha}^{{tree}}"], repo).stdout.strip()
    if not target_tree:
        return False
    with tempfile.TemporaryDirectory(prefix="code-sync-index-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        for args in (["read-tree", commit_sha], ["add", "-u"]):
            if _git(args, repo, env=env).returncode != 0:
                return False
        actual_tree = _git(["write-tree"], repo, env=env).stdout.strip()
    return bool(actual_tree) and actual_tree == target_tree


def reconcile_repo(
    repo: Path,
    *,
    folder_id: str,
    repo_relpath: str,
    remote_url: str,
    auth_header: str | None = None,
    conflicts_path: Path | None = None,
) -> RepoReconcileResult:
    """Fetch one repo's current branch from a peer and fast-forward if safe."""

    def result(action: str, branch: str = "", detail: str = "") -> RepoReconcileResult:
        return RepoReconcileResult(
            folder_id=folder_id,
            repo_relpath=repo_relpath,
            branch=branch,
            action=action,
            detail=detail,
        )

    if operation_in_progress(repo):
        return result(ACTION_SKIPPED_IN_PROGRESS)
    branch = current_branch(repo)
    if branch is None:
        return result(ACTION_SKIPPED_DETACHED)

    fetch = _git(
        ["fetch", "--quiet", remote_url, branch], repo, env=_auth_env(auth_header)
    )
    if fetch.returncode != 0:
        return result(ACTION_FETCH_FAILED, branch, fetch.stderr.strip()[:200])

    # --verify: plain rev-parse echoes the ref name itself when it cannot
    # resolve, which once minted a conflict record against a "remote" sha
    # of literally FETCH_HEAD.
    fetched_sha = _git(
        ["rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}"], repo
    ).stdout.strip()
    local_sha = _git(["rev-parse", f"refs/heads/{branch}"], repo).stdout.strip()
    if not fetched_sha or not local_sha:
        return result(ACTION_FETCH_FAILED, branch, "could not resolve heads")
    if fetched_sha == local_sha:
        mark_branch_conflicts_resolved(
            folder_id=folder_id,
            repo_relpath=repo_relpath,
            branch=branch,
            path=conflicts_path,
        )
        return result(ACTION_UP_TO_DATE, branch)

    local_is_ancestor = (
        _git(["merge-base", "--is-ancestor", local_sha, fetched_sha], repo).returncode
        == 0
    )
    if local_is_ancestor:
        if _git(["diff", "--cached", "--quiet"], repo).returncode != 0:
            # Staged work means an agent is mid-change here; the mixed reset
            # below would silently unstage it. Wait for a quiet tick.
            return result(ACTION_SKIPPED_IN_PROGRESS, branch, "staged changes present")
        if not worktree_matches_commit(repo, fetched_sha):
            # Files are still arriving via Syncthing; try again next tick.
            return result(ACTION_AWAITING_FILES, branch)
        update = _git(
            ["update-ref", f"refs/heads/{branch}", fetched_sha, local_sha], repo
        )
        if update.returncode != 0:
            return result(ACTION_FETCH_FAILED, branch, update.stderr.strip()[:200])
        # Refresh the index to the (already matching) new HEAD.
        _git(["reset", "--quiet"], repo)
        mark_branch_conflicts_resolved(
            folder_id=folder_id,
            repo_relpath=repo_relpath,
            branch=branch,
            path=conflicts_path,
        )
        return result(
            ACTION_FAST_FORWARDED, branch, f"{local_sha[:12]} -> {fetched_sha[:12]}"
        )

    remote_is_ancestor = (
        _git(["merge-base", "--is-ancestor", fetched_sha, local_sha], repo).returncode
        == 0
    )
    if remote_is_ancestor:
        return result(ACTION_REMOTE_BEHIND, branch)

    record_branch_conflict(
        folder_id=folder_id,
        repo_relpath=repo_relpath,
        branch=branch,
        local_sha=local_sha,
        remote_sha=fetched_sha,
        path=conflicts_path,
    )
    return result(
        ACTION_DIVERGED, branch, f"local {local_sha[:12]} vs remote {fetched_sha[:12]}"
    )


def scan_file_conflicts(
    folder: SyncFolder,
    home: Path | None = None,
    conflicts_path: Path | None = None,
) -> list[str]:
    """Record Syncthing ``*.sync-conflict-*`` copies inside a folder."""
    from openbase_coder_cli.code_sync.repositories import (
        is_repository_manifest_conflict,
    )

    folder_root = folder.absolute_path(home)
    found: list[str] = []
    if not folder_root.is_dir():
        return found
    for path in folder_root.rglob("*.sync-conflict-*"):
        if not path.is_file():
            continue
        if is_repository_manifest_conflict(path):
            # Simultaneous branch/commit changes can race two manifests. The
            # canonical Syncthing winner drives convergence and the losing
            # commit is retained in a recovery ref, so this metadata copy is
            # redundant and should not become a user-facing file conflict.
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if _is_generated_file_conflict_path(path, folder_root):
            continue
        relpath = str(path.relative_to(folder_root))
        record_file_conflict(
            folder_id=folder.folder_id, file_relpath=relpath, path=conflicts_path
        )
        found.append(relpath)
    # Filesystem is the source of truth: any previously-recorded copy the scan
    # no longer sees (deleted, resolved on a peer, or cleaned up out of band)
    # stops counting as an open conflict. Without this the ledger only ever
    # grew and the dashboard showed phantom conflicts for long-gone files.
    reconcile_file_conflicts_against_disk(
        folder_id=folder.folder_id, active_relpaths=found, path=conflicts_path
    )
    return found


def _is_generated_file_conflict_path(path: Path, folder_root: Path) -> bool:
    """Generated/cache conflict copies should not become user-facing records."""
    try:
        rel_parts = path.relative_to(folder_root).parts
    except ValueError:
        rel_parts = path.parts
    if any(part in SKIP_DIR_NAMES or part == ".git" for part in rel_parts):
        return True
    return any(
        len(rel_parts) >= len(segment)
        and any(
            tuple(rel_parts[index : index + len(segment)]) == segment
            for index in range(len(rel_parts) - len(segment) + 1)
        )
        for segment in SKIP_FILE_CONFLICT_PATH_SEGMENTS
    )


def run_reconcile_once(
    *,
    config_path: Path | None = None,
    home: Path | None = None,
    conflicts_path: Path | None = None,
    peers: tuple[SyncPeer, ...] | None = None,
) -> dict[str, Any]:
    """One reconcile tick across all synced folders and peers."""
    summary: dict[str, Any] = {
        "repos": [],
        "file_conflicts": [],
        "errors": [],
    }
    if peers is None:
        peers = syncable_peers(current_eligibility())
    if not peers:
        summary["errors"].append("no syncable peers advertised")

    auth_header = None
    if peers:
        try:
            token = TokenManager(web_backend_url()).get_access_token()
            auth_header = f"Bearer {token}"
        except (AuthLoginRequiredError, AuthTransientError) as exc:
            summary["errors"].append(f"auth: {exc}")
            peers = ()

    from openbase_coder_cli.code_sync.repositories import (
        adopt_repository,
        repository_state,
        sync_checkout_manifest,
    )
    from openbase_coder_cli.code_sync.worktrees import (
        adopt_worktree,
        ensure_worktree_manifest,
    )

    reconcile_state = read_reconcile_state()
    previous_repo_states = reconcile_state.get("repo_states")
    if not isinstance(previous_repo_states, dict):
        previous_repo_states = {}
    next_repo_states: dict[str, dict[str, str]] = {}

    for folder in sync_folders(config_path):
        folder_root = folder.absolute_path(home)
        summary["file_conflicts"].extend(
            scan_file_conflicts(folder, home, conflicts_path)
        )
        repos, worktree_candidates, repo_candidates = discover_repos_and_candidates(
            folder_root
        )
        for candidate in worktree_candidates:
            rel = str(candidate.relative_to(folder_root))
            remote = peer_git_url(peers[0], folder.folder_id, rel) if peers else None
            try:
                action = adopt_worktree(
                    candidate, home=home, remote_url=remote, auth_header=auth_header
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                action = f"adopt_error: {exc}"
            summary.setdefault("worktrees", []).append({"path": rel, "action": action})
        for candidate in repo_candidates:
            rel = str(candidate.relative_to(folder_root))
            if rel == ".":
                rel = ""
            try:
                action = adopt_repository(
                    candidate,
                    remote_urls=(
                        peer_git_url(peer, folder.folder_id, rel) for peer in peers
                    ),
                    auth_header=auth_header,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                action = f"adopt_error: {exc}"
            summary.setdefault("repository_bootstrap", []).append(
                {"path": rel, "action": action}
            )
        if repo_candidates:
            repos = discover_git_repos(folder_root)
        for repo in repos:
            try:
                is_worktree = ensure_worktree_manifest(repo, home)
            except (OSError, subprocess.TimeoutExpired):
                is_worktree = False
            repo_relpath = str(repo.relative_to(folder_root))
            if repo_relpath == ".":
                repo_relpath = ""
            state_key = f"{folder.folder_id}:{repo_relpath}"
            previous_state = previous_repo_states.get(state_key)
            manifest_action = sync_checkout_manifest(
                repo,
                is_worktree=is_worktree,
                home=home,
                previous_state=(
                    previous_state if isinstance(previous_state, dict) else None
                ),
                remote_urls=(
                    peer_git_url(peer, folder.folder_id, repo_relpath) for peer in peers
                ),
                auth_header=auth_header,
            )
            manifest_summary_key = (
                "worktree_manifests" if is_worktree else "repository_manifests"
            )
            summary.setdefault(manifest_summary_key, []).append(
                {"path": repo_relpath, "action": manifest_action}
            )
            for peer in peers:
                try:
                    outcome = reconcile_repo(
                        repo,
                        folder_id=folder.folder_id,
                        repo_relpath=repo_relpath,
                        remote_url=peer_git_url(peer, folder.folder_id, repo_relpath),
                        auth_header=auth_header,
                        conflicts_path=conflicts_path,
                    )
                except subprocess.TimeoutExpired:
                    # One hung repo must not abort the rest of the tick.
                    summary["errors"].append(
                        f"git timed out in {folder.folder_id}/{repo_relpath}"
                    )
                    continue
                summary["repos"].append({"peer": peer.name, **asdict(outcome)})
            final_state = repository_state(repo)
            if final_state is not None:
                next_repo_states[state_key] = final_state

    summary["conflicts_count"] = len(unresolved_conflicts(conflicts_path))
    write_reconcile_state(
        {
            **reconcile_state,
            "last_reconcile_at": _timestamp(),
            "repo_states": next_repo_states,
            **_counts(summary),
        }
    )
    return summary


def run_tick_if_enabled() -> dict[str, Any] | None:
    """Reconcile + lease tick, or ``None`` when code sync is disabled."""
    try:
        enabled = code_sync_enabled()
    except ValueError:
        return None
    if not enabled:
        return None
    from openbase_coder_cli.code_sync import CodeSyncError
    from openbase_coder_cli.code_sync.lease import run_lease_tick
    from openbase_coder_cli.code_sync.manager import (
        accept_pending_folders,
        apply_settings_change,
        ensure_product_state_folders,
    )

    eligibility = current_eligibility()
    peers = syncable_peers(eligibility)
    _refresh_config_if_peers_changed(eligibility, peers)
    # First-class product folders (thread sync, agent-home skills) migrate
    # into the managed engine automatically on existing installs. A
    # user-managed Syncthing still carrying them makes apply refuse (the
    # overlap guard); keep ticking and let the health warning surface it.
    product_folders_error = ""
    try:
        if ensure_product_state_folders():
            apply_settings_change()
    except CodeSyncError as exc:
        product_folders_error = str(exc)
    adopted = accept_pending_folders()
    summary = run_reconcile_once(peers=peers)
    if adopted:
        summary["adopted_folders"] = adopted
    if product_folders_error:
        summary["errors"].append(f"product folders: {product_folders_error}")
    summary["lease"] = run_lease_tick()
    return summary


def _refresh_config_if_peers_changed(eligibility, peers) -> None:
    """Re-render the Syncthing config when the syncable peer set changes.

    New devices register their ``syncthing_device_id`` capability after this
    device last rendered config.xml (e.g. a freshly provisioned DevSpace);
    this picks them up without requiring a settings mutation.
    """
    from openbase_coder_cli.code_sync import CodeSyncError
    from openbase_coder_cli.code_sync import manager as sync_manager

    rendered_ids = sorted(peer.syncthing_device_id for peer in peers)
    state = read_reconcile_state()
    if state.get("rendered_peer_ids") == rendered_ids:
        return
    try:
        sync_manager.render_configuration(eligibility)
        sync_manager.restart_service_if_installed()
    except (CodeSyncError, OSError):
        return
    write_reconcile_state({**state, "rendered_peer_ids": rendered_ids})


def read_reconcile_state(path: Path | None = None) -> dict[str, Any]:
    state_path = path or RECONCILE_STATE_PATH
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_reconcile_state(state: dict[str, Any], path: Path | None = None) -> None:
    state_path = path or RECONCILE_STATE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=state_path.parent, delete=False
    ) as tmp:
        json.dump(state, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, state_path)


def _counts(summary: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in summary["repos"]:
        counts[entry["action"]] = counts.get(entry["action"], 0) + 1
    return {
        "repo_count": len(summary["repos"]),
        "fast_forwarded": counts.get(ACTION_FAST_FORWARDED, 0),
        "diverged": counts.get(ACTION_DIVERGED, 0),
    }


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
