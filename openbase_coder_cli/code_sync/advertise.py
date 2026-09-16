"""Advertise sibling-worktree branches into their trunk repository.

Multi-workspace worktrees live at ``<workspace>-worktrees/<task>/<repo>``,
sibling to the trunk checkout ``<workspace>/<repo>``. On the machine that
created one, it is a linked git worktree and shares refs with the trunk.
On a sync peer, code-sync hydrates the synced files as a STANDALONE
repository (``.git`` never syncs), so its branches and commits are
invisible from the peer's trunk: the branch does not exist there,
``git worktree list`` shows nothing, and an agent hunting for a commit it
knows was made on the other machine finds nothing (2026-09-16 incident:
``ead6dcf`` on ``fix/voice-duplicate-response`` was only reachable inside
the hydrated clone).

This module closes the gap on every reconcile tick by fetching such
branches from the hydrated checkout into the trunk repo:

- a branch name the trunk does not use is imported as a real local branch
  (``refs/heads/<branch>``), so normal tooling — ``git branch``,
  ``git log <branch>``, ``git fetch <branch>`` — just sees it;
- a trunk branch strictly behind the worktree's tip is fast-forwarded
  (non-forced fetch, refused by git if the branch is checked out anywhere);
- anything that cannot be applied to the real branch name (true divergence,
  or new commits on a branch currently checked out in the trunk) is
  mirrored under the ``synced/`` branch namespace
  (``refs/heads/synced/<branch>``), which is visible in ``git branch``
  without ever displacing a real local branch. Mirrors are force-updated
  to track the worktree's tip; real branch names are never force-moved.

The trunk's checked-out branch and working tree are never touched — only
refs move, and only through fetch semantics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

WORKTREES_DIR_SUFFIX = "-worktrees"
SYNCED_BRANCH_NAMESPACE = "synced"

ACTION_IMPORTED = "imported"
ACTION_FAST_FORWARDED = "fast_forwarded"
ACTION_MIRRORED = "mirrored"
ACTION_SKIPPED_ORIGIN_MISMATCH = "skipped_origin_mismatch"
ACTION_FETCH_FAILED = "fetch_failed"


def sibling_trunk_repo(repo: Path) -> Path | None:
    """Trunk checkout for a repo under a ``<workspace>-worktrees`` sibling.

    ``.../<ws>-worktrees/<task>/<sub...>`` maps to ``.../<ws>/<sub...>``,
    and ``.../<ws>-worktrees/<task>`` (single-repo layout) to ``.../<ws>``.
    Returns ``None`` unless the mapped path exists and holds a git repo.
    """
    parts = repo.parts
    for index in range(len(parts) - 1, -1, -1):
        name = parts[index]
        if not name.endswith(WORKTREES_DIR_SUFFIX) or name == WORKTREES_DIR_SUFFIX:
            continue
        if index + 1 >= len(parts):
            continue  # The -worktrees dir itself, not a task inside it.
        workspace = name[: -len(WORKTREES_DIR_SUFFIX)]
        trunk = Path(*parts[:index], workspace, *parts[index + 2 :])
        if trunk == repo:
            continue
        try:
            if (trunk / ".git").exists():
                return trunk
        except OSError:
            return None
        return None
    return None


def advertise_sibling_branches(repo: Path) -> list[dict[str, Any]]:
    """Make ``repo``'s local branches visible in its sibling trunk repo.

    ``repo`` is any discovered checkout; the call is a cheap no-op unless it
    sits under a ``*-worktrees`` sibling of an existing trunk repo and is a
    standalone repository (a linked worktree already shares the trunk's
    refs). Returns one entry per branch acted on (or blocked); silent for
    branches the trunk already has.
    """
    from openbase_coder_cli.code_sync.worktrees import is_linked_worktree

    trunk = sibling_trunk_repo(repo)
    if trunk is None:
        return []
    if is_linked_worktree(repo):
        return []  # Shared refs: the trunk already sees these branches.

    branches = _local_branches(repo)
    if not branches:
        return []

    # Cheap pre-filter without the worktree's objects: equal tips and
    # current mirrors are the steady state and need no fetch at all.
    candidates: list[tuple[str, str, str]] = []  # (branch, tip, trunk_tip)
    for branch, tip in branches:
        trunk_tip = _rev_parse_commit(trunk, f"refs/heads/{branch}")
        if trunk_tip == tip:
            continue
        mirror = _rev_parse_commit(
            trunk, f"refs/heads/{SYNCED_BRANCH_NAMESPACE}/{branch}"
        )
        if mirror == tip:
            continue  # Already mirrored at this exact tip.
        candidates.append((branch, tip, trunk_tip))
    if not candidates:
        return []

    if not _origins_match(repo, trunk):
        # Never fetch history into a repo we cannot prove is the same
        # project. Reported (not silent) because there IS something to
        # advertise and the identity check is what blocks it.
        return [
            {
                "branch": branch,
                "action": ACTION_SKIPPED_ORIGIN_MISMATCH,
                "detail": f"worktree {repo} origin does not match trunk {trunk}",
            }
            for branch, _tip, _trunk_tip in candidates
        ]

    checked_out = _checked_out_branches(trunk)
    results: list[dict[str, Any]] = []
    for branch, _tip, trunk_tip in candidates:
        results.append(_advertise_branch(repo, trunk, branch, trunk_tip, checked_out))
    return [entry for entry in results if entry]


def _advertise_branch(
    repo: Path,
    trunk: Path,
    branch: str,
    trunk_tip: str,
    checked_out: set[str],
) -> dict[str, Any] | None:
    """Fetch one branch's objects into the trunk, then move refs safely.

    The fetch targets FETCH_HEAD only (no ref update), so ancestry can be
    decided with the objects actually present; refs then move through
    compare-and-swap ``update-ref`` (create-only for imports, old-tip-
    guarded for fast-forwards), never displacing a real local branch.
    """
    from openbase_coder_cli.code_sync.reconciler import _git

    def failed(detail: str) -> dict[str, Any]:
        return {"branch": branch, "action": ACTION_FETCH_FAILED, "detail": detail}

    fetch = _git(
        ["fetch", "--quiet", "--no-tags", str(repo), f"refs/heads/{branch}"], trunk
    )
    if fetch.returncode != 0:
        return failed(fetch.stderr.strip()[:200])
    tip = _rev_parse_commit(trunk, "FETCH_HEAD")
    if not tip:
        return failed("fetched tip unresolvable")

    if trunk_tip == tip or (trunk_tip and _is_ancestor(trunk, tip, trunk_tip)):
        return None  # Trunk already contains everything the worktree has.
    if not trunk_tip and _reachable_from_a_branch(trunk, tip):
        return None  # Branch was merged and deleted here; do not resurrect.

    can_touch_real_ref = branch not in checked_out
    if not trunk_tip and can_touch_real_ref:
        # Create-only: the empty old value makes update-ref refuse if the
        # branch appeared since we looked.
        update = _git(["update-ref", f"refs/heads/{branch}", tip, ""], trunk)
        if update.returncode != 0:
            return failed(update.stderr.strip()[:200])
        return {
            "branch": branch,
            "action": ACTION_IMPORTED,
            "detail": f"created {branch} at {tip[:12]} from {repo}",
        }
    if trunk_tip and can_touch_real_ref and _is_ancestor(trunk, trunk_tip, tip):
        update = _git(["update-ref", f"refs/heads/{branch}", tip, trunk_tip], trunk)
        if update.returncode != 0:
            return failed(update.stderr.strip()[:200])
        return {
            "branch": branch,
            "action": ACTION_FAST_FORWARDED,
            "detail": f"{trunk_tip[:12]} -> {tip[:12]}",
        }

    # Diverged, or the branch is checked out in the trunk: mirror under the
    # synced/ namespace instead of touching the real branch.
    mirror_name = f"{SYNCED_BRANCH_NAMESPACE}/{branch}"
    if mirror_name in checked_out:
        return failed(f"mirror branch {mirror_name} is checked out")
    update = _git(["update-ref", f"refs/heads/{mirror_name}", tip], trunk)
    if update.returncode != 0:
        return failed(update.stderr.strip()[:200])
    reason = "checked out in trunk" if branch in checked_out else "diverged from trunk"
    return {
        "branch": branch,
        "action": ACTION_MIRRORED,
        "detail": (
            f"{mirror_name} -> {tip[:12]} "
            f"({reason}{f', trunk at {trunk_tip[:12]}' if trunk_tip else ''})"
        ),
    }


def _local_branches(repo: Path) -> list[tuple[str, str]]:
    from openbase_coder_cli.code_sync.reconciler import _git

    result = _git(
        ["for-each-ref", "--format=%(objectname) %(refname:short)", "refs/heads"],
        repo,
    )
    if result.returncode != 0:
        return []
    branches: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        sha, _, name = line.partition(" ")
        if sha and name and not name.startswith(f"{SYNCED_BRANCH_NAMESPACE}/"):
            branches.append((name, sha))
    return branches


def _checked_out_branches(trunk: Path) -> set[str]:
    """Branch names checked out in the trunk or any of its linked worktrees."""
    from openbase_coder_cli.code_sync.reconciler import _git

    result = _git(["worktree", "list", "--porcelain"], trunk)
    if result.returncode != 0:
        return set()
    prefix = "branch refs/heads/"
    return {
        line.removeprefix(prefix)
        for line in result.stdout.splitlines()
        if line.startswith(prefix)
    }


def _rev_parse_commit(repo: Path, ref: str) -> str:
    from openbase_coder_cli.code_sync.reconciler import _git

    return _git(
        ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo
    ).stdout.strip()


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    from openbase_coder_cli.code_sync.reconciler import _git

    return (
        _git(["merge-base", "--is-ancestor", ancestor, descendant], repo).returncode
        == 0
    )


def _reachable_from_a_branch(trunk: Path, sha: str) -> bool:
    from openbase_coder_cli.code_sync.reconciler import _git

    if _git(["cat-file", "-e", f"{sha}^{{commit}}"], trunk).returncode != 0:
        return False
    contains = _git(
        ["for-each-ref", "--count=1", f"--contains={sha}", "refs/heads"], trunk
    )
    return contains.returncode == 0 and bool(contains.stdout.strip())


def _origins_match(repo: Path, trunk: Path) -> bool:
    """Both checkouts must advertise the same origin (transport-agnostic)."""
    ours = _normalized_origin(repo)
    theirs = _normalized_origin(trunk)
    return bool(ours) and ours == theirs


def _normalized_origin(repo: Path) -> str:
    from openbase_coder_cli.code_sync.reconciler import _git

    result = _git(["remote", "get-url", "origin"], repo)
    if result.returncode != 0:
        return ""
    url = result.stdout.strip()
    if not url:
        return ""
    if "://" in url:
        parsed = urlsplit(url)
        host, path = parsed.hostname or "", parsed.path
    elif "@" in url and ":" in url:  # scp-like git@host:org/repo.git
        _, _, rest = url.partition("@")
        host, _, path = rest.partition(":")
    else:
        host, path = "", url
    path = path.strip("/")
    path = path.removesuffix(".git")
    return f"{host.lower()}/{path}" if host else path
