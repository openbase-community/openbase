"""Heal working trees dirtied by sync echoes of already-pushed commits.

Machine A commits and pushes; Syncthing carries the working-tree file
changes to machine B, whose git HEAD is still behind — so B's checkout
shows those files as modified/deleted/untracked even though every byte is
exactly what origin's newer commit contains. Agents (correctly paranoid
about clobbering someone's uncommitted work) then refuse to touch the
repo, and each such repo had to be hand-verified and fast-forwarded
(2026-09-16 incident).

``heal_repo_echo`` proves the dirt is a pure echo and fast-forwards the
repo with ``git reset --hard <verified origin sha>`` ONLY under exactly
these conditions, any one of which failing aborts the heal for the repo:

- the repo is on a branch, with no merge/rebase/etc. in progress and
  nothing staged (staged work means an agent is mid-change);
- ``origin/<current branch>`` resolves (freshly fetched when allowed,
  else the cached remote-tracking ref) and local HEAD is STRICTLY behind
  it;
- every unstaged-dirty tracked path's working-tree state is byte-identical
  to the same path in the origin commit (deleted locally ⇔ absent in
  origin);
- every untracked file that exists in the origin commit's tree — i.e.
  everything ``reset --hard`` would overwrite — is byte-identical too;
  all other untracked files are untouched by the reset and left alone;
- the status snapshot is unchanged when re-taken immediately before the
  reset (an agent starting work mid-check aborts the heal).

The reset never switches branches — it fast-forwards the checked-out
branch to history that is provably already on origin.

Known cosmetic artifact: hydrated sibling worktrees can show ``uv.lock``
/ ``pnpm-lock.yaml`` as deleted (the origin machine keeps them as
symlinks, which Syncthing does not carry). Those deletions are classified
explicitly (``known_lockfile_artifact`` when they are the only dirt) so
agents read them as noise, and they are never "restored" here — writing a
real file back would sync to the other machine and clobber its symlink.

Origin fetches are rate-limited per repo (a busy machine has many
legitimately-dirty repos every tick); between fetches the cached
``refs/remotes/origin/<branch>`` is used, which is always safe — every
condition is proven against whatever sha is actually applied.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

KNOWN_LOCKFILE_ARTIFACT_NAMES = {"uv.lock", "pnpm-lock.yaml"}
DEFAULT_FETCH_MIN_INTERVAL_SECONDS = 300.0
LS_TREE_CHUNK = 500

ACTION_HEALED = "healed"
ACTION_ECHO_DETECTED = "echo_detected"  # apply=False (detection-only mode)
ACTION_NOT_ECHO = "not_echo"
ACTION_SKIPPED_STAGED = "skipped_staged"
ACTION_LOCKFILE_ARTIFACT = "known_lockfile_artifact"
ACTION_CHANGED_DURING_CHECK = "changed_during_check"
ACTION_RESET_FAILED = "reset_failed"
# Actions that are ordinary life on a dev machine; the reconcile tick does
# not record them (a dirty repo that is simply not behind origin, etc.).
SILENT_ACTIONS = frozenset(
    {
        "clean",
        "untracked_only",
        "skipped_detached",
        "skipped_in_progress",
        "no_origin_branch",
        "not_behind",
        "status_unreadable",
    }
)

_last_fetch_attempts: dict[str, float] = {}


def heal_repo_echo(
    repo: Path,
    *,
    allow_fetch: bool = True,
    min_fetch_interval: float | None = None,
    apply: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    """Detect (and, when ``apply``, heal) pure sync-echo dirt in ``repo``.

    Returns ``{"action": ..., "branch": ..., "detail": ...}``.
    """
    from openbase_coder_cli.code_sync.reconciler import (
        _git,
        current_branch,
        operation_in_progress,
    )

    def result(action: str, branch: str = "", detail: str = "") -> dict[str, Any]:
        return {"action": action, "branch": branch, "detail": detail}

    if operation_in_progress(repo):
        return result("skipped_in_progress")
    branch = current_branch(repo)
    if branch is None:
        return result("skipped_detached")

    snapshot = _status_snapshot(repo)
    if snapshot is None:
        return result("status_unreadable", branch)
    if not snapshot.raw:
        return result("clean", branch)
    if snapshot.unparseable:
        return result(
            ACTION_NOT_ECHO,
            branch,
            f"unrecognized status entry: {snapshot.unparseable}",
        )
    if snapshot.staged:
        return result(
            ACTION_SKIPPED_STAGED, branch, f"staged changes ({snapshot.staged[0]})"
        )
    if not snapshot.dirty:
        return result("untracked_only", branch)

    lockfile_deletions = [
        path
        for path, code in snapshot.dirty
        if code == "D" and Path(path).name in KNOWN_LOCKFILE_ARTIFACT_NAMES
    ]
    if len(lockfile_deletions) == len(snapshot.dirty):
        # The ubiquitous hydrated-worktree artifact; classify without even
        # fetching. Cosmetic — see module docstring for why not restored.
        return result(
            ACTION_LOCKFILE_ARTIFACT,
            branch,
            "lockfile deletions are a known sync artifact "
            f"({', '.join(sorted(lockfile_deletions))}); safe to ignore",
        )

    target_sha, target_source = _origin_target(
        repo,
        branch,
        allow_fetch=allow_fetch,
        min_fetch_interval=min_fetch_interval,
        now=now,
    )
    if not target_sha:
        return result("no_origin_branch", branch)
    head_sha = _git(
        ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], repo
    ).stdout.strip()
    if not head_sha:
        return result("status_unreadable", branch, "HEAD unresolvable")
    if head_sha == target_sha or not _is_ancestor(repo, head_sha, target_sha):
        return result("not_behind", branch)

    # Every path reset --hard could touch must already equal the target:
    # unstaged-dirty tracked paths, plus untracked files the target tracks.
    dirty_paths = [path for path, _code in snapshot.dirty]
    try:
        entries = _target_entries(repo, target_sha, dirty_paths + snapshot.untracked)
    except _TargetUnreadableError as exc:
        return result("status_unreadable", branch, str(exc)[:200])
    for path in dirty_paths:
        matches, why = _worktree_matches_target(repo, path, entries.get(path))
        if not matches:
            return result(ACTION_NOT_ECHO, branch, _mismatch_detail(path, why))
    for path in snapshot.untracked:
        entry = entries.get(path)
        if entry is None:
            continue  # reset --hard leaves untracked files alone.
        matches, why = _worktree_matches_target(repo, path, entry)
        if not matches:
            return result(
                ACTION_NOT_ECHO, branch, _mismatch_detail(path, f"untracked; {why}")
            )

    detail = (
        f"{head_sha[:12]} -> {target_sha[:12]} ({target_source}); "
        f"{len(dirty_paths)} dirty file(s) byte-identical to origin/{branch}"
    )
    if not apply:
        return result(ACTION_ECHO_DETECTED, branch, detail + "; safe to fast-forward")

    # Close the window between proof and reset: an agent touching the repo
    # in the meantime aborts the heal.
    recheck = _status_snapshot(repo)
    if recheck is None or recheck.raw != snapshot.raw:
        return result(ACTION_CHANGED_DURING_CHECK, branch)
    reset = _git(["reset", "--hard", "--quiet", target_sha], repo)
    if reset.returncode != 0:
        return result(ACTION_RESET_FAILED, branch, reset.stderr.strip()[:200])
    return result(ACTION_HEALED, branch, detail)


def _mismatch_detail(path: str, why: str) -> str:
    known = Path(path).name in KNOWN_LOCKFILE_ARTIFACT_NAMES
    suffix = " [known lockfile sync artifact]" if known else ""
    return f"file {path} differs from origin ({why}){suffix}"


class _StatusSnapshot:
    def __init__(
        self,
        raw: str,
        staged: list[str],
        dirty: list[tuple[str, str]],
        untracked: list[str],
        unparseable: str,
    ) -> None:
        self.raw = raw
        self.staged = staged
        self.dirty = dirty  # (path, worktree status code)
        self.untracked = untracked
        self.unparseable = unparseable


def _status_snapshot(repo: Path) -> _StatusSnapshot | None:
    from openbase_coder_cli.code_sync.reconciler import _git

    result = _git(
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--no-renames",
        ],
        repo,
    )
    if result.returncode != 0:
        return None
    staged: list[str] = []
    dirty: list[tuple[str, str]] = []
    untracked: list[str] = []
    unparseable = ""
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            return None  # Not porcelain v1 shape; refuse to interpret.
        index_code, worktree_code, path = entry[0], entry[1], entry[3:]
        if index_code == "?" and worktree_code == "?":
            untracked.append(path)
        elif index_code not in " ":
            staged.append(path)
        elif worktree_code in "MDT":
            dirty.append((path, worktree_code))
        elif worktree_code != " ":
            unparseable = f"{index_code}{worktree_code} {path}"
    return _StatusSnapshot(result.stdout, staged, dirty, untracked, unparseable)


class _TargetUnreadableError(Exception):
    pass


def _target_entries(
    repo: Path, target_sha: str, paths: list[str]
) -> dict[str, tuple[str, str]]:
    """``path -> (mode, blob sha)`` in the target commit, for given paths."""
    from openbase_coder_cli.code_sync.reconciler import _git

    entries: dict[str, tuple[str, str]] = {}
    unique = list(dict.fromkeys(paths))
    for start in range(0, len(unique), LS_TREE_CHUNK):
        chunk = unique[start : start + LS_TREE_CHUNK]
        specs = [f":(literal){path}" for path in chunk]
        result = _git(["ls-tree", "-r", "-z", target_sha, "--", *specs], repo)
        if result.returncode != 0:
            raise _TargetUnreadableError(
                f"ls-tree failed: {result.stderr.strip()[:120]}"
            )
        for line in result.stdout.split("\0"):
            if not line:
                continue
            meta, _, path = line.partition("\t")
            fields = meta.split(" ")
            if len(fields) != 3 or not path:
                continue
            mode, _obj_type, sha = fields
            entries[path] = (mode, sha)
    return entries


def _worktree_matches_target(
    repo: Path, path: str, entry: tuple[str, str] | None
) -> tuple[bool, str]:
    """Whether the working-tree state of ``path`` equals the target commit's.

    ``entry`` is the target's ``(mode, blob sha)`` or ``None`` when the
    target does not track the path. Type changes, submodules, and anything
    else unusual fail closed. The executable bit alone is not compared —
    the reset converges it to the target either way.
    """
    from openbase_coder_cli.code_sync.reconciler import _git

    full = repo / path
    exists = os.path.lexists(full)
    if not exists:
        if entry is None:
            return True, ""
        return False, "deleted locally but present in origin"
    if entry is None:
        # reset --hard would DELETE this local file (target drops it).
        return False, "present locally but absent in origin"
    mode, target_blob = entry
    if os.path.islink(full):
        if mode != "120000":
            return False, "symlink locally, regular file in origin"
        # A symlink blob is its target string; hash it via stdin (a plain
        # hash-object on the path would hash the pointed-to file instead).
        proc = subprocess.run(
            ["git", "hash-object", "--stdin"],
            cwd=repo,
            input=os.readlink(full),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        local_blob = proc.stdout.strip() if proc.returncode == 0 else ""
        return (
            (local_blob == target_blob),
            "" if local_blob == target_blob else "symlink target differs",
        )
    if full.is_dir():
        return False, "directory in working tree"
    if mode == "120000":
        return False, "regular file locally, symlink in origin"
    if mode == "160000":
        return False, "submodule"
    hashed = _git(["hash-object", "--", path], repo)
    local_blob = hashed.stdout.strip() if hashed.returncode == 0 else ""
    if not local_blob:
        return False, "unhashable"
    return (
        (local_blob == target_blob),
        "" if local_blob == target_blob else "content differs",
    )


def _origin_target(
    repo: Path,
    branch: str,
    *,
    allow_fetch: bool,
    min_fetch_interval: float | None,
    now: float | None,
) -> tuple[str, str]:
    """Resolve the origin sha to heal toward: fresh fetch, else cached ref."""
    from openbase_coder_cli.code_sync.reconciler import _git

    if allow_fetch and _fetch_due(repo, min_fetch_interval, now):
        fetch = _git(["fetch", "--quiet", "origin", branch], repo)
        if fetch.returncode == 0:
            sha = _git(
                ["rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}"], repo
            ).stdout.strip()
            if sha:
                return sha, "origin fetched"
    tracking = _git(
        [
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/remotes/origin/{branch}^{{commit}}",
        ],
        repo,
    ).stdout.strip()
    if tracking:
        return tracking, "cached origin tracking ref"
    return "", ""


def _fetch_due(repo: Path, min_fetch_interval: float | None, now: float | None) -> bool:
    interval = (
        DEFAULT_FETCH_MIN_INTERVAL_SECONDS
        if min_fetch_interval is None
        else min_fetch_interval
    )
    moment = time.monotonic() if now is None else now
    key = str(repo)
    last = _last_fetch_attempts.get(key)
    if last is not None and moment - last < interval:
        return False
    _last_fetch_attempts[key] = moment
    return True


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    from openbase_coder_cli.code_sync.reconciler import _git

    return (
        _git(["merge-base", "--is-ancestor", ancestor, descendant], repo).returncode
        == 0
    )
