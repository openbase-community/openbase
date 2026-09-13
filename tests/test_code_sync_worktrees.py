from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from openbase_coder_cli import sync_config
from openbase_coder_cli.code_sync import reconciler, repositories, worktrees
from openbase_coder_cli.code_sync.eligibility import SyncPeer

GIT_IDENTITY = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _make_main_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], capture_output=True, check=True
    )
    (path / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "initial")
    return path


def _commit_worktree_file(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _copy_files_without_git(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            shutil.copytree(item, dst / item.name)
        else:
            shutil.copy2(item, dst / item.name)


def _origin_with_worktree(home: Path) -> tuple[Path, Path]:
    main = _make_main_repo(home / "Projects" / "app")
    wt = home / "Projects" / "app-worktrees" / "dev"
    _git(main, "worktree", "add", "-b", "dev", str(wt))
    (wt / "feature.py").write_text("work\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "dev work")
    return main, wt


def test_manifest_written_for_worktree_only(tmp_path: Path) -> None:
    main, wt = _origin_with_worktree(tmp_path)

    # Detection is read-only and manifest-free.
    assert worktrees.is_linked_worktree(main) is False
    assert worktrees.is_linked_worktree(wt) is True
    assert not (wt / worktrees.WORKTREE_MANIFEST_NAME).exists()

    assert worktrees.ensure_worktree_manifest(main, home=tmp_path) is False
    assert not (main / worktrees.WORKTREE_MANIFEST_NAME).exists()

    assert worktrees.ensure_worktree_manifest(wt, home=tmp_path) is True
    manifest = worktrees.read_manifest(wt)
    assert manifest["main_repo"] == "Projects/app"
    assert manifest["branch"] == "dev"
    assert manifest["head"] == _git(wt, "rev-parse", "HEAD")
    # Excluded from status via the machine-local exclude, not .gitignore.
    assert _git(wt, "status", "--porcelain") == ""


def test_discovery_reports_unattached_worktree_dirs(tmp_path: Path) -> None:
    main, wt = _origin_with_worktree(tmp_path)
    worktrees.ensure_worktree_manifest(wt, home=tmp_path)
    peer_dir = tmp_path / "peer-folder" / "app-worktrees" / "dev"
    _copy_files_without_git(wt, peer_dir)

    repos, candidates = reconciler.discover_repos_and_worktree_candidates(
        tmp_path / "peer-folder"
    )
    assert candidates == [peer_dir]
    assert peer_dir not in repos


def test_adopt_attaches_worktree_and_reconciles(tmp_path: Path) -> None:
    origin_home = tmp_path / "origin"
    peer_home = tmp_path / "peer"
    main, wt = _origin_with_worktree(origin_home)
    worktrees.ensure_worktree_manifest(wt, home=origin_home)

    # Peer has its own clone of the main repo (with the dev branch) and the
    # worktree arrived as synced files + manifest, no .git.
    peer_main = peer_home / "Projects" / "app"
    peer_main.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", str(main), str(peer_main)],
        capture_output=True,
        check=True,
    )
    _git(peer_main, "branch", "dev", "origin/dev")
    peer_wt = peer_home / "Projects" / "app-worktrees" / "dev"
    _copy_files_without_git(wt, peer_wt)

    action = worktrees.adopt_worktree(peer_wt, home=peer_home)

    assert action == "adopted"
    assert (peer_wt / ".git").is_file()
    assert _git(peer_wt, "rev-parse", "--abbrev-ref", "HEAD") == "dev"
    assert _git(peer_wt, "status", "--porcelain") == ""
    assert _git(peer_wt, "rev-parse", "HEAD") == _git(wt, "rev-parse", "HEAD")

    # The money path: a commit in the origin worktree fast-forwards the
    # peer's worktree branch once the files have synced.
    (wt / "feature.py").write_text("work v2\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "more dev work")
    (peer_wt / "feature.py").write_text("work v2\n", encoding="utf-8")

    outcome = reconciler.reconcile_repo(
        peer_wt,
        folder_id="cs-test",
        repo_relpath="app-worktrees/dev",
        remote_url=str(wt),
        conflicts_path=tmp_path / "conflicts.json",
    )
    assert outcome.action == reconciler.ACTION_FAST_FORWARDED
    assert _git(peer_wt, "rev-parse", "HEAD") == _git(wt, "rev-parse", "HEAD")
    assert _git(peer_wt, "status", "--porcelain") == ""


def test_adopt_fetches_missing_branch_from_peer(tmp_path: Path) -> None:
    origin_home = tmp_path / "origin"
    peer_home = tmp_path / "peer"
    main, wt = _origin_with_worktree(origin_home)
    worktrees.ensure_worktree_manifest(wt, home=origin_home)

    peer_main = peer_home / "Projects" / "app"
    peer_main.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", str(main), str(peer_main)],
        capture_output=True,
        check=True,
    )  # No local dev branch created.
    peer_wt = peer_home / "Projects" / "app-worktrees" / "dev"
    _copy_files_without_git(wt, peer_wt)

    assert worktrees.adopt_worktree(peer_wt, home=peer_home) == "branch_missing_no_peer"
    action = worktrees.adopt_worktree(peer_wt, home=peer_home, remote_url=str(wt))
    assert action == "adopted"
    assert _git(peer_wt, "rev-parse", "HEAD") == _git(wt, "rev-parse", "HEAD")


def test_existing_worktree_follows_synced_branch_manifest(tmp_path: Path) -> None:
    origin_home = tmp_path / "origin"
    peer_home = tmp_path / "peer"
    main, wt = _origin_with_worktree(origin_home)
    worktrees.ensure_worktree_manifest(wt, home=origin_home)
    peer_main = peer_home / "Projects" / "app"
    peer_main.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", str(main), str(peer_main)],
        capture_output=True,
        check=True,
    )
    _git(peer_main, "branch", "dev", "origin/dev")
    peer_wt = peer_home / "Projects" / "app-worktrees" / "dev"
    _copy_files_without_git(wt, peer_wt)
    assert worktrees.adopt_worktree(peer_wt, home=peer_home) == "adopted"

    _git(wt, "checkout", "-q", "-b", "feature")
    head = _commit_worktree_file(wt, "feature.py", "feature v2\n", "feature branch")
    worktrees.ensure_worktree_manifest(wt, home=origin_home)
    _copy_files_without_git(wt, peer_wt)
    manifest = worktrees.read_manifest(peer_wt)
    assert manifest is not None

    action = repositories.converge_repository_to_manifest(
        peer_wt, manifest, remote_urls=(str(wt),)
    )

    assert action == "converged"
    assert _git(peer_wt, "rev-parse", "--abbrev-ref", "HEAD") == "feature"
    assert _git(peer_wt, "rev-parse", "HEAD") == head


def test_reconcile_tick_follows_worktree_rewrite_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    # Regression: the tick used to refresh the worktree manifest during
    # detection, overwriting a peer-published manifest with local state
    # before it could be consumed — worktree branch moves and rewrites
    # never converged through the manifest path.
    from openbase_coder_cli.code_sync.conflicts import unresolved_conflicts

    home = tmp_path / "home"
    main, wt = _origin_with_worktree(home)
    old_tip = _git(wt, "rev-parse", "HEAD")

    # The peer machine's stand-in: a clone that rewrites the dev branch.
    peer = tmp_path / "peer"
    subprocess.run(
        ["git", "clone", "-q", str(main), str(peer)],
        capture_output=True,
        check=True,
    )
    _git(peer, "checkout", "-q", "dev")

    config_path = tmp_path / "sync-config.json"
    conflicts_path = tmp_path / "conflicts.json"
    sync_config.set_sync_folders([{"relpath": "Projects"}], config_path)
    sync_peer = SyncPeer("peer", "peer", "desktop", "peer.test", "engine")
    monkeypatch.setattr(
        reconciler, "RECONCILE_STATE_PATH", tmp_path / "reconcile-state.json"
    )
    monkeypatch.setattr(
        reconciler.TokenManager, "get_access_token", lambda _self: "token"
    )
    monkeypatch.setattr(reconciler, "peer_git_url", lambda *_args: str(peer))

    def tick():
        return reconciler.run_reconcile_once(
            config_path=config_path,
            home=home,
            conflicts_path=conflicts_path,
            peers=(sync_peer,),
        )

    tick()  # Publishes manifests and records per-repo previous state.

    _git(peer, "commit", "--amend", "-m", "dev work (rewritten)")
    new_tip = _git(peer, "rev-parse", "HEAD")
    # Syncthing stand-in: the rewriting peer's worktree manifest arrives.
    (wt / worktrees.WORKTREE_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "main_repo": "Projects/app",
                "branch": "dev",
                "head": new_tip,
                "replaces": [old_tip],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = tick()

    (manifest_entry,) = summary["worktree_manifests"]
    assert manifest_entry["path"] == "app-worktrees/dev"
    assert manifest_entry["action"].startswith("converged_rewrite; backup=")
    assert _git(wt, "rev-parse", "HEAD") == new_tip
    assert unresolved_conflicts(conflicts_path) == []


def test_peer_worktree_manifest_survives_blocked_convergence(tmp_path: Path) -> None:
    # When convergence cannot run yet (staged changes), the peer's manifest
    # must stay on disk untouched so a later tick can still consume it.
    home = tmp_path / "home"
    main, wt = _origin_with_worktree(home)
    old_tip = _git(wt, "rev-parse", "HEAD")
    peer_head = "f" * 40
    (wt / worktrees.WORKTREE_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "main_repo": "Projects/app",
                "branch": "dev",
                "head": peer_head,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (wt / "staged.txt").write_text("keep staged\n", encoding="utf-8")
    _git(wt, "add", "staged.txt")

    action = repositories.sync_checkout_manifest(
        wt,
        is_worktree=True,
        home=home,
        previous_state={"branch": "dev", "head": old_tip},
        remote_urls=(),
        auth_header=None,
    )

    assert action == "staged_changes"
    manifest = worktrees.read_manifest(wt)
    assert manifest is not None
    assert manifest["head"] == peer_head


def test_adopt_skips_when_unsafe(tmp_path: Path) -> None:
    origin_home = tmp_path / "origin"
    peer_home = tmp_path / "peer"
    main, wt = _origin_with_worktree(origin_home)
    worktrees.ensure_worktree_manifest(wt, home=origin_home)
    peer_wt = peer_home / "Projects" / "app-worktrees" / "dev"
    _copy_files_without_git(wt, peer_wt)

    # Main repo missing on this machine.
    assert worktrees.adopt_worktree(peer_wt, home=peer_home) == "main_repo_missing"

    # Branch already checked out in the peer's main checkout.
    peer_main = peer_home / "Projects" / "app"
    subprocess.run(
        ["git", "clone", "-q", str(main), str(peer_main)],
        capture_output=True,
        check=True,
    )
    _git(peer_main, "checkout", "-q", "-b", "dev", "origin/dev")
    assert (
        worktrees.adopt_worktree(peer_wt, home=peer_home)
        == "branch_checked_out_elsewhere"
    )
