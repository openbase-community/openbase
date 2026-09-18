"""Sibling-worktree branch advertisement into the trunk repo.

A hydrated multi-workspace worktree is a standalone clone on the peer
machine; its branches/commits must become visible in the sibling trunk
checkout without ever touching the trunk's checked-out branch or files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from openbase_coder_cli.code_sync import advertise

GIT_IDENTITY = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]
ORIGIN_URL = "https://github.com/example/app"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _make_trunk(path: Path, origin_url: str = ORIGIN_URL) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)], capture_output=True, check=True
    )
    (path / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "initial")
    _git(path, "remote", "add", "origin", origin_url)
    return path


def _make_hydrated_clone(trunk: Path, dest: Path, origin_url: str = ORIGIN_URL) -> Path:
    """A standalone clone standing in for a code-sync hydrated worktree."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", str(trunk), str(dest)], capture_output=True, check=True
    )
    _git(dest, "remote", "set-url", "origin", origin_url)
    return dest


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    trunk = _make_trunk(tmp_path / "code" / "ws" / "app")
    clone = _make_hydrated_clone(
        trunk, tmp_path / "code" / "ws-worktrees" / "task" / "app"
    )
    return trunk, clone


def test_sibling_trunk_repo_mapping(tmp_path: Path) -> None:
    trunk, clone = _layout(tmp_path)
    assert advertise.sibling_trunk_repo(clone) == trunk
    # Single-repo layout: <ws>-worktrees/<task> -> <ws>.
    single_trunk = _make_trunk(tmp_path / "code" / "site")
    single = _make_hydrated_clone(
        single_trunk, tmp_path / "code" / "site-worktrees" / "fix"
    )
    assert advertise.sibling_trunk_repo(single) == single_trunk
    # Trunk repos themselves and unrelated paths do not map.
    assert advertise.sibling_trunk_repo(trunk) is None
    assert advertise.sibling_trunk_repo(tmp_path / "code" / "elsewhere") is None
    # Mapping requires the trunk to exist as a git repo.
    orphan = _make_hydrated_clone(
        trunk, tmp_path / "code" / "missing-worktrees" / "task" / "app"
    )
    assert advertise.sibling_trunk_repo(orphan) is None


def test_new_branch_imported_as_local_branch(tmp_path: Path) -> None:
    trunk, clone = _layout(tmp_path)
    trunk_head_before = _git(trunk, "rev-parse", "HEAD")
    _git(clone, "checkout", "-qb", "fix/voice-duplicate-response")
    tip = _commit(clone, "fix.py", "fix\n", "the fix")

    results = advertise.advertise_sibling_branches(clone)

    assert [(r["branch"], r["action"]) for r in results] == [
        ("fix/voice-duplicate-response", advertise.ACTION_IMPORTED)
    ]
    assert _git(trunk, "rev-parse", "fix/voice-duplicate-response") == tip
    # Trunk checkout untouched: same HEAD, same branch, clean tree.
    assert _git(trunk, "rev-parse", "HEAD") == trunk_head_before
    assert _git(trunk, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(trunk, "status", "--porcelain") == ""
    # Second tick is silent.
    assert advertise.advertise_sibling_branches(clone) == []


def test_existing_branch_fast_forwarded(tmp_path: Path) -> None:
    trunk, clone = _layout(tmp_path)
    _git(clone, "checkout", "-qb", "feature")
    old_tip = _commit(clone, "f.py", "one\n", "c1")
    _git(trunk, "fetch", "-q", str(clone), "refs/heads/feature:refs/heads/feature")
    assert _git(trunk, "rev-parse", "feature") == old_tip
    new_tip = _commit(clone, "f.py", "two\n", "c2")

    results = advertise.advertise_sibling_branches(clone)

    assert [(r["branch"], r["action"]) for r in results] == [
        ("feature", advertise.ACTION_FAST_FORWARDED)
    ]
    assert _git(trunk, "rev-parse", "feature") == new_tip


def test_checked_out_branch_is_mirrored_not_moved(tmp_path: Path) -> None:
    trunk, clone = _layout(tmp_path)
    trunk_head = _git(trunk, "rev-parse", "HEAD")
    tip = _commit(clone, "main.py", "new on main\n", "main work")  # clone on main

    results = advertise.advertise_sibling_branches(clone)

    assert [(r["branch"], r["action"]) for r in results] == [
        ("main", advertise.ACTION_MIRRORED)
    ]
    assert _git(trunk, "rev-parse", "refs/heads/synced/main") == tip
    assert _git(trunk, "rev-parse", "HEAD") == trunk_head
    assert _git(trunk, "status", "--porcelain") == ""
    # Mirror is current: silent on the next tick.
    assert advertise.advertise_sibling_branches(clone) == []


def test_diverged_branch_is_mirrored_and_tracks_rewrites(tmp_path: Path) -> None:
    trunk, clone = _layout(tmp_path)
    _git(clone, "checkout", "-qb", "topic")
    _commit(clone, "t.py", "clone side\n", "clone c1")
    _git(trunk, "branch", "topic")  # Same name, then diverge in trunk.
    _git(trunk, "checkout", "-q", "topic")
    trunk_tip = _commit(trunk, "t.py", "trunk side\n", "trunk c1")
    _git(trunk, "checkout", "-q", "main")

    results = advertise.advertise_sibling_branches(clone)
    assert [(r["branch"], r["action"]) for r in results] == [
        ("topic", advertise.ACTION_MIRRORED)
    ]
    assert _git(trunk, "rev-parse", "topic") == trunk_tip  # Real branch untouched.
    clone_tip = _git(clone, "rev-parse", "topic")
    assert _git(trunk, "rev-parse", "refs/heads/synced/topic") == clone_tip

    # The mirror force-updates when the worktree rewrites its history.
    _git(clone, "commit", "-q", "--amend", "-m", "clone c1 (amended)")
    amended = _git(clone, "rev-parse", "topic")
    results = advertise.advertise_sibling_branches(clone)
    assert [(r["branch"], r["action"]) for r in results] == [
        ("topic", advertise.ACTION_MIRRORED)
    ]
    assert _git(trunk, "rev-parse", "refs/heads/synced/topic") == amended


def test_origin_mismatch_blocks_all_advertisement(tmp_path: Path) -> None:
    trunk = _make_trunk(tmp_path / "code" / "ws" / "app")
    clone = _make_hydrated_clone(
        trunk,
        tmp_path / "code" / "ws-worktrees" / "task" / "app",
        origin_url="https://github.com/other/project",
    )
    _git(clone, "checkout", "-qb", "feature")
    _commit(clone, "f.py", "x\n", "c1")

    results = advertise.advertise_sibling_branches(clone)

    assert [r["action"] for r in results] == [advertise.ACTION_SKIPPED_ORIGIN_MISMATCH]
    assert "feature" not in _git(trunk, "branch", "--list", "feature")


def test_origin_transport_variants_still_match(tmp_path: Path) -> None:
    trunk = _make_trunk(
        tmp_path / "code" / "ws" / "app", origin_url="git@github.com:example/app.git"
    )
    clone = _make_hydrated_clone(
        trunk,
        tmp_path / "code" / "ws-worktrees" / "task" / "app",
        origin_url="https://github.com/example/app",
    )
    _git(clone, "checkout", "-qb", "feature")
    tip = _commit(clone, "f.py", "x\n", "c1")

    results = advertise.advertise_sibling_branches(clone)

    assert [r["action"] for r in results] == [advertise.ACTION_IMPORTED]
    assert _git(trunk, "rev-parse", "feature") == tip


def test_merged_and_deleted_branch_is_not_resurrected(tmp_path: Path) -> None:
    trunk, clone = _layout(tmp_path)
    _git(clone, "checkout", "-qb", "done")
    tip = _commit(clone, "d.py", "done\n", "finish work")
    # Trunk merges the branch (fast-forward) and deletes it.
    _git(trunk, "fetch", "-q", str(clone), "refs/heads/done:refs/heads/done")
    _git(trunk, "merge", "-q", "--ff-only", "done")
    _git(trunk, "branch", "-q", "-d", "done")
    assert _git(trunk, "rev-parse", "HEAD") == tip

    assert advertise.advertise_sibling_branches(clone) == []
    assert _git(trunk, "branch", "--list", "done") == ""


def test_trunk_ahead_is_silent(tmp_path: Path) -> None:
    trunk, clone = _layout(tmp_path)
    _git(clone, "checkout", "-qb", "feature")
    _commit(clone, "f.py", "one\n", "c1")
    _git(trunk, "fetch", "-q", str(clone), "refs/heads/feature:refs/heads/feature")
    _git(trunk, "checkout", "-q", "feature")
    _commit(trunk, "f.py", "two\n", "trunk moved ahead")
    _git(trunk, "checkout", "-q", "main")

    assert advertise.advertise_sibling_branches(clone) == []


def test_linked_worktree_is_a_noop(tmp_path: Path) -> None:
    trunk = _make_trunk(tmp_path / "code" / "ws" / "app")
    wt = tmp_path / "code" / "ws-worktrees" / "task" / "app"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(trunk, "worktree", "add", "-q", "-b", "dev", str(wt))
    _commit(wt, "dev.py", "dev\n", "dev work")

    # Linked worktrees share refs with the trunk; nothing to advertise.
    assert advertise.advertise_sibling_branches(wt) == []


def test_reconcile_tick_advertises_hydrated_worktree_branches(
    tmp_path: Path, monkeypatch
) -> None:
    from openbase_coder_cli import sync_config
    from openbase_coder_cli.code_sync import reconciler

    home = tmp_path / "home"
    trunk = _make_trunk(home / "code" / "ws" / "app")
    clone = _make_hydrated_clone(trunk, home / "code" / "ws-worktrees" / "task" / "app")
    _git(clone, "checkout", "-qb", "feature")
    tip = _commit(clone, "f.py", "x\n", "c1")

    config_path = tmp_path / "sync-config.json"
    sync_config.set_sync_folders([{"relpath": "code"}], config_path)
    monkeypatch.setattr(
        reconciler, "RECONCILE_STATE_PATH", tmp_path / "reconcile-state.json"
    )

    summary = reconciler.run_reconcile_once(
        config_path=config_path,
        home=home,
        conflicts_path=tmp_path / "conflicts.json",
        peers=(),
    )

    adverts = summary.get("trunk_advertisements", [])
    assert [(a["branch"], a["action"]) for a in adverts] == [
        ("feature", advertise.ACTION_IMPORTED)
    ]
    assert adverts[0]["path"] == "ws-worktrees/task/app"
    assert _git(trunk, "rev-parse", "feature") == tip
    counts = reconciler.reconcile_counts(summary)
    assert counts["advertised"] == 1
