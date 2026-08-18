from __future__ import annotations

import subprocess
from pathlib import Path

from openbase_coder_cli.code_sync import conflicts as conflicts_module
from openbase_coder_cli.code_sync import reconciler

GIT_IDENTITY = [
    "-c",
    "user.email=test@example.com",
    "-c",
    "user.name=Test",
]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        capture_output=True,
        check=True,
    )
    return path


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _pair(tmp_path: Path) -> tuple[Path, Path]:
    """A local repo and a peer clone sharing one initial commit."""
    local = _init_repo(tmp_path / "local")
    _commit(local, "app.py", "print('v1')\n", "initial")
    peer = tmp_path / "peer"
    subprocess.run(
        ["git", "clone", "--quiet", str(local), str(peer)],
        capture_output=True,
        check=True,
    )
    return local, peer


def _reconcile(local: Path, peer: Path, conflicts_path: Path):
    return reconciler.reconcile_repo(
        local,
        folder_id="cs-test",
        repo_relpath="local",
        remote_url=str(peer),
        conflicts_path=conflicts_path,
    )


def test_fast_forward_when_ancestor_and_worktree_matches(tmp_path: Path) -> None:
    local, peer = _pair(tmp_path)
    peer_head = _commit(peer, "app.py", "print('v2')\n", "peer change")
    # Simulate Syncthing having already delivered the file content.
    (local / "app.py").write_text("print('v2')\n", encoding="utf-8")

    result = _reconcile(local, peer, tmp_path / "conflicts.json")

    assert result.action == reconciler.ACTION_FAST_FORWARDED
    assert _git(local, "rev-parse", "main") == peer_head
    assert _git(local, "status", "--porcelain") == ""
    assert conflicts_module.unresolved_conflicts(tmp_path / "conflicts.json") == []


def test_fast_forward_when_peer_commit_adds_a_new_file(tmp_path: Path) -> None:
    """Syncthing-delivered new files are untracked locally; ff must still fire."""
    local, peer = _pair(tmp_path)
    (peer / "extra.py").write_text("print('new')\n", encoding="utf-8")
    _git(peer, "add", "-A")
    _git(peer, "commit", "-m", "peer adds a file")
    peer_head = _git(peer, "rev-parse", "HEAD")
    # Simulate Syncthing having already delivered the new file content.
    (local / "extra.py").write_text("print('new')\n", encoding="utf-8")

    result = _reconcile(local, peer, tmp_path / "conflicts.json")

    assert result.action == reconciler.ACTION_FAST_FORWARDED
    assert _git(local, "rev-parse", "main") == peer_head
    assert _git(local, "status", "--porcelain") == ""


def test_gitignored_secrets_do_not_block_fast_forward(tmp_path: Path) -> None:
    local, peer = _pair(tmp_path)
    peer_head = _commit(peer, ".gitignore", ".env\n", "ignore env")
    (local / ".gitignore").write_text(".env\n", encoding="utf-8")
    (local / ".env").write_text("SECRET=only-here\n", encoding="utf-8")

    result = _reconcile(local, peer, tmp_path / "conflicts.json")

    assert result.action == reconciler.ACTION_FAST_FORWARDED
    assert _git(local, "rev-parse", "main") == peer_head
    assert (local / ".env").read_text(encoding="utf-8") == "SECRET=only-here\n"


def test_staged_changes_defer_fast_forward(tmp_path: Path) -> None:
    local, peer = _pair(tmp_path)
    _commit(peer, "app.py", "print('v2')\n", "peer change")
    (local / "app.py").write_text("print('v2')\n", encoding="utf-8")
    (local / "wip.py").write_text("work in progress\n", encoding="utf-8")
    _git(local, "add", "wip.py")
    local_head = _git(local, "rev-parse", "main")

    result = _reconcile(local, peer, tmp_path / "conflicts.json")

    assert result.action == reconciler.ACTION_SKIPPED_IN_PROGRESS
    assert _git(local, "rev-parse", "main") == local_head
    # The staged entry survives untouched.
    assert "A  wip.py" in _git(local, "status", "--porcelain")


def test_waits_when_ancestor_but_files_still_arriving(tmp_path: Path) -> None:
    local, peer = _pair(tmp_path)
    _commit(peer, "app.py", "print('v2')\n", "peer change")
    local_head = _git(local, "rev-parse", "main")

    result = _reconcile(local, peer, tmp_path / "conflicts.json")

    assert result.action == reconciler.ACTION_AWAITING_FILES
    assert _git(local, "rev-parse", "main") == local_head
    assert conflicts_module.unresolved_conflicts(tmp_path / "conflicts.json") == []


def test_diverged_branches_record_a_conflict(tmp_path: Path) -> None:
    local, peer = _pair(tmp_path)
    local_head = _commit(local, "app.py", "print('local')\n", "local change")
    peer_head = _commit(peer, "app.py", "print('peer')\n", "peer change")
    conflicts_path = tmp_path / "conflicts.json"

    result = _reconcile(local, peer, conflicts_path)

    assert result.action == reconciler.ACTION_DIVERGED
    assert _git(local, "rev-parse", "main") == local_head
    records = conflicts_module.unresolved_conflicts(conflicts_path)
    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "branch"
    assert record["branch"] == "main"
    assert record["local_sha"] == local_head
    assert record["remote_sha"] == peer_head

    # A second tick dedupes instead of stacking records.
    _reconcile(local, peer, conflicts_path)
    assert len(conflicts_module.unresolved_conflicts(conflicts_path)) == 1

    # Manifest-driven convergence (or a manual resolution) clears the stale
    # record as soon as both machines advertise the same branch head.
    _git(local, "reset", "--hard", peer_head)
    assert (
        _reconcile(local, peer, conflicts_path).action == reconciler.ACTION_UP_TO_DATE
    )
    assert conflicts_module.unresolved_conflicts(conflicts_path) == []


def test_mid_merge_repo_is_never_touched(tmp_path: Path) -> None:
    local, peer = _pair(tmp_path)
    peer_head = _commit(peer, "app.py", "print('v2')\n", "peer change")
    git_dir = local / ".git"
    (git_dir / "MERGE_HEAD").write_text(peer_head + "\n", encoding="utf-8")

    result = _reconcile(local, peer, tmp_path / "conflicts.json")

    assert result.action == reconciler.ACTION_SKIPPED_IN_PROGRESS


def test_up_to_date_and_remote_behind(tmp_path: Path) -> None:
    local, peer = _pair(tmp_path)
    assert (
        _reconcile(local, peer, tmp_path / "c.json").action
        == reconciler.ACTION_UP_TO_DATE
    )

    _commit(local, "app.py", "print('ahead')\n", "local ahead")
    assert (
        _reconcile(local, peer, tmp_path / "c.json").action
        == reconciler.ACTION_REMOTE_BEHIND
    )


def test_resolve_use_remote_stashes_then_resets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    local_parent = home / "Projects" / "demo"
    local_parent.mkdir(parents=True)
    local = _init_repo(local_parent / "local")
    _commit(local, "app.py", "print('v1')\n", "initial")
    peer = tmp_path / "peer"
    subprocess.run(
        ["git", "clone", "--quiet", str(local), str(peer)],
        capture_output=True,
        check=True,
    )
    _commit(local, "app.py", "print('local')\n", "local change")
    peer_head = _commit(peer, "app.py", "print('peer')\n", "peer change")
    (local / "untracked.txt").write_text("keep me\n", encoding="utf-8")

    conflicts_path = tmp_path / "conflicts.json"
    config_path = tmp_path / "sync-config.json"
    from openbase_coder_cli import sync_config

    sync_config.set_sync_folders([{"relpath": "Projects/demo"}], config_path)
    folder_id = sync_config.folder_id_for_relpath("Projects/demo")

    result = reconciler.reconcile_repo(
        local,
        folder_id=folder_id,
        repo_relpath="local",
        remote_url=str(peer),
        conflicts_path=conflicts_path,
    )
    assert result.action == reconciler.ACTION_DIVERGED
    record = conflicts_module.unresolved_conflicts(conflicts_path)[0]

    resolved = conflicts_module.resolve_conflict(
        record["id"],
        "use_remote",
        path=conflicts_path,
        home=home,
        config_path=config_path,
    )

    assert resolved["resolved"] is True
    assert _git(local, "rev-parse", "HEAD") == peer_head
    # The pre-reset worktree survives in the safety stash.
    assert "code-sync-backup" in _git(local, "stash", "list")
    assert conflicts_module.unresolved_conflicts(conflicts_path) == []


def test_resolve_keep_local_leaves_repo_alone(tmp_path: Path) -> None:
    conflicts_path = tmp_path / "conflicts.json"
    record = conflicts_module.record_branch_conflict(
        folder_id="cs-test",
        repo_relpath="local",
        branch="main",
        local_sha="a" * 40,
        remote_sha="b" * 40,
        path=conflicts_path,
    )

    resolved = conflicts_module.resolve_conflict(
        record["id"], "keep_local", path=conflicts_path
    )

    assert resolved["resolved"] is True
    assert resolved["resolution"] == "keep_local"


def test_discover_git_repos_respects_depth_and_skips_noise(tmp_path: Path) -> None:
    root = tmp_path / "folder"
    _init_repo(root / "repo-a")
    _init_repo(root / "group" / "repo-b")
    _init_repo(root / "node_modules" / "dep")  # skipped
    _init_repo(root / "d1" / "d2" / "d3" / "d4" / "d5" / "too-deep")

    repos = reconciler.discover_git_repos(root)

    assert root / "repo-a" in repos
    assert root / "group" / "repo-b" in repos
    assert all("node_modules" not in str(repo) for repo in repos)
    assert all("too-deep" not in str(repo) for repo in repos)


def test_discover_git_repos_includes_nested_multi_workspace_subrepos(
    tmp_path: Path,
) -> None:
    """A workspace repo's subrepos (own .git each) reconcile too."""
    root = tmp_path / "folder"
    workspace = _init_repo(root / "workspace")
    subrepo = _init_repo(workspace / "cli")

    repos = reconciler.discover_git_repos(root)

    assert workspace in repos
    assert subrepo in repos


def test_scan_file_conflicts_records_sync_conflict_copies(tmp_path: Path) -> None:
    from openbase_coder_cli.sync_config import SyncFolder

    home = tmp_path / "home"
    folder = SyncFolder(relpath="Projects/demo")
    folder_root = folder.absolute_path(home)
    folder_root.mkdir(parents=True)
    (folder_root / "notes.sync-conflict-20260706-101112-ABCDEF.md").write_text(
        "conflict copy\n", encoding="utf-8"
    )

    conflicts_path = tmp_path / "conflicts.json"
    found = reconciler.scan_file_conflicts(folder, home, conflicts_path)

    assert found == ["notes.sync-conflict-20260706-101112-ABCDEF.md"]
    records = conflicts_module.unresolved_conflicts(conflicts_path)
    assert len(records) == 1
    assert records[0]["kind"] == "file"


def test_scan_file_conflicts_resolves_records_for_vanished_copies(
    tmp_path: Path,
) -> None:
    from openbase_coder_cli.sync_config import SyncFolder

    home = tmp_path / "home"
    folder = SyncFolder(relpath="Projects/demo")
    folder_root = folder.absolute_path(home)
    folder_root.mkdir(parents=True)
    copy = folder_root / "notes.sync-conflict-20260706-101112-ABCDEF.md"
    copy.write_text("conflict copy\n", encoding="utf-8")

    conflicts_path = tmp_path / "conflicts.json"
    reconciler.scan_file_conflicts(folder, home, conflicts_path)
    assert len(conflicts_module.unresolved_conflicts(conflicts_path)) == 1

    # The copy is cleaned up out of band; the next scan must treat the
    # filesystem as authoritative and stop counting the phantom record.
    copy.unlink()
    found = reconciler.scan_file_conflicts(folder, home, conflicts_path)

    assert found == []
    assert conflicts_module.unresolved_conflicts(conflicts_path) == []
    # History is retained, just marked resolved rather than deleted.
    all_records = conflicts_module.read_conflicts(conflicts_path)
    assert len(all_records) == 1
    assert all_records[0]["resolved"] is True
    assert all_records[0]["resolution"] == "disappeared"


def test_reconcile_file_conflicts_adds_and_resolves_in_one_pass(
    tmp_path: Path,
) -> None:
    conflicts_path = tmp_path / "conflicts.json"
    added, resolved = conflicts_module.reconcile_file_conflicts(
        folder_id="cs-demo",
        active_relpaths=["a.sync-conflict-x.md", "b.sync-conflict-x.md"],
        path=conflicts_path,
    )
    assert (added, resolved) == (2, 0)

    # Re-running with a shrunk active set adds nothing and retires the missing
    # one, deduping the still-present copy rather than re-adding it.
    added, resolved = conflicts_module.reconcile_file_conflicts(
        folder_id="cs-demo",
        active_relpaths=["a.sync-conflict-x.md"],
        path=conflicts_path,
    )
    assert (added, resolved) == (0, 1)
    unresolved = conflicts_module.unresolved_conflicts(conflicts_path)
    assert [c["path"] for c in unresolved] == ["a.sync-conflict-x.md"]


def test_compact_conflicts_bounds_resolved_history(tmp_path: Path) -> None:
    conflicts_path = tmp_path / "conflicts.json"
    # Seed 5 resolved records (varying resolved_at) and 1 unresolved.
    records = [
        {
            "id": f"r{i}",
            "kind": "file",
            "folder_id": "cs-demo",
            "path": f"f{i}",
            "resolved": True,
            "resolved_at": f"2026-08-1{i}T00:00:00Z",
        }
        for i in range(5)
    ]
    records.append(
        {
            "id": "live",
            "kind": "file",
            "folder_id": "cs-demo",
            "path": "live",
            "resolved": False,
        }
    )
    conflicts_module._write_conflicts(records, conflicts_path)

    removed = conflicts_module.compact_conflicts(conflicts_path, max_resolved=2)
    assert removed == 3

    remaining = conflicts_module.read_conflicts(conflicts_path)
    ids = {c["id"] for c in remaining}
    # Unresolved always kept; only the 2 most-recent resolved survive.
    assert "live" in ids
    assert {"r4", "r3"}.issubset(ids)
    assert not any(rid in ids for rid in ("r0", "r1", "r2"))
    # Idempotent once at/under the cap.
    assert conflicts_module.compact_conflicts(conflicts_path, max_resolved=2) == 0


def test_scan_file_conflicts_skips_generated_artifacts(tmp_path: Path) -> None:
    from openbase_coder_cli.sync_config import SyncFolder

    home = tmp_path / "home"
    folder = SyncFolder(relpath="Projects/demo")
    folder_root = folder.absolute_path(home)
    generated_conflicts = [
        folder_root / ".stversions/repo/file.sync-conflict-20260706-101112-ABC.md",
        folder_root / "repo/.local/logs/app.sync-conflict-20260706-101112-ABC.log",
        folder_root / "repo/logs/launchd/app.sync-conflict-20260706-101112-ABC.log",
        folder_root / "repo/data/db/base/123.sync-conflict-20260706-101112-ABC",
        folder_root
        / "repo/desktop/companion-build/app.sync-conflict-20260706-101112-ABC",
        folder_root / "repo/__pycache__/mod.sync-conflict-20260706-101112-ABC.pyc",
    ]
    for path in generated_conflicts:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated\n", encoding="utf-8")

    conflicts_path = tmp_path / "conflicts.json"
    found = reconciler.scan_file_conflicts(folder, home, conflicts_path)

    assert found == []
    assert conflicts_module.unresolved_conflicts(conflicts_path) == []


def test_scan_file_conflicts_cleans_internal_repo_manifest_copy(tmp_path: Path) -> None:
    from openbase_coder_cli.sync_config import SyncFolder

    home = tmp_path / "home"
    folder = SyncFolder(relpath="Projects/demo")
    folder_root = folder.absolute_path(home)
    folder_root.mkdir(parents=True)
    conflict_copy = (
        folder_root / ".openbase-repo.sync-conflict-20260706-101112-ABCDEF.json"
    )
    conflict_copy.write_text("{}\n", encoding="utf-8")

    found = reconciler.scan_file_conflicts(folder, home, tmp_path / "conflicts.json")

    assert found == []
    assert not conflict_copy.exists()


def test_discover_git_repos_skips_unreadable_directories(tmp_path: Path) -> None:
    import os

    root = tmp_path / "folder"
    _init_repo(root / "repo-a")
    locked = root / "locked"
    locked.mkdir(parents=True)
    os.chmod(locked, 0o000)
    try:
        repos = reconciler.discover_git_repos(root)
    finally:
        os.chmod(locked, 0o755)

    assert root / "repo-a" in repos


def test_extra_untracked_files_do_not_block_fast_forward(tmp_path: Path) -> None:
    """In-flight uncommitted work is the normal state of a live machine."""
    local, peer = _pair(tmp_path)
    peer_head = _commit(peer, "app.py", "print('v2')\n", "peer change")
    (local / "app.py").write_text("print('v2')\n", encoding="utf-8")
    (local / "wip-notes.md").write_text("uncommitted work\n", encoding="utf-8")

    result = _reconcile(local, peer, tmp_path / "conflicts.json")

    assert result.action == reconciler.ACTION_FAST_FORWARDED
    assert _git(local, "rev-parse", "main") == peer_head
    # The in-flight file survives, still untracked.
    assert (local / "wip-notes.md").read_text(encoding="utf-8") == "uncommitted work\n"
    assert "?? wip-notes.md" in _git(local, "status", "--porcelain")


def test_discover_git_repos_skips_trash_dirs(tmp_path: Path) -> None:
    """Trash never syncs (Syncthing-ignored), so it must never reconcile."""
    root = tmp_path / "folder"
    _init_repo(root / "repo-a")
    _init_repo(root / "trash" / "old-project")
    _init_repo(root / "Trash" / "older-project")

    repos = reconciler.discover_git_repos(root)

    assert repos == [root / "repo-a"]


def test_one_broken_repo_does_not_abort_the_tick(tmp_path: Path, monkeypatch) -> None:
    from openbase_coder_cli import sync_config
    from openbase_coder_cli.code_sync import repositories

    home = tmp_path / "home"
    alpha = _init_repo(home / "Projects" / "alpha")
    _commit(alpha, "a.py", "print('a')\n", "initial")
    beta = _init_repo(home / "Projects" / "beta")
    _commit(beta, "b.py", "print('b')\n", "initial")
    config_path = tmp_path / "sync-config.json"
    sync_config.set_sync_folders([{"relpath": "Projects"}], config_path)
    monkeypatch.setattr(
        reconciler, "RECONCILE_STATE_PATH", tmp_path / "reconcile-state.json"
    )
    real_sync_checkout_manifest = repositories.sync_checkout_manifest

    def broken_for_alpha(repo: Path, **kwargs):
        if repo.name == "alpha":
            raise subprocess.TimeoutExpired(cmd="git", timeout=60)
        return real_sync_checkout_manifest(repo, **kwargs)

    monkeypatch.setattr(repositories, "sync_checkout_manifest", broken_for_alpha)

    summary = reconciler.run_reconcile_once(
        config_path=config_path,
        home=home,
        conflicts_path=tmp_path / "conflicts.json",
        peers=(),
    )

    assert any(
        "alpha" in error and "TimeoutExpired" in error for error in summary["errors"]
    )
    assert {"path": "beta", "action": "published"} in summary["repository_manifests"]


def test_trash_branch_conflicts_are_retired(tmp_path: Path, monkeypatch) -> None:
    from openbase_coder_cli import sync_config

    home = tmp_path / "home"
    _init_repo(home / "Projects" / "trash" / "junk")
    config_path = tmp_path / "sync-config.json"
    conflicts_path = tmp_path / "conflicts.json"
    sync_config.set_sync_folders([{"relpath": "Projects"}], config_path)
    folder = sync_config.sync_folders(config_path)[0]
    conflicts_module.record_branch_conflict(
        folder_id=folder.folder_id,
        repo_relpath="trash/junk",
        branch="main",
        local_sha="a" * 40,
        remote_sha="b" * 40,
        path=conflicts_path,
    )
    monkeypatch.setattr(
        reconciler, "RECONCILE_STATE_PATH", tmp_path / "reconcile-state.json"
    )

    summary = reconciler.run_reconcile_once(
        config_path=config_path,
        home=home,
        conflicts_path=conflicts_path,
        peers=(),
    )

    assert summary["conflicts_retired_skipped"] == 1
    assert conflicts_module.unresolved_conflicts(conflicts_path) == []
    assert summary.get("repository_manifests", []) == []


def test_run_reconcile_once_persists_last_summary(tmp_path: Path, monkeypatch) -> None:
    from openbase_coder_cli import sync_config

    home = tmp_path / "home"
    repo = _init_repo(home / "Projects" / "demo")
    _commit(repo, "app.py", "print('v1')\n", "initial")
    config_path = tmp_path / "sync-config.json"
    state_path = tmp_path / "reconcile-state.json"
    sync_config.set_sync_folders([{"relpath": "Projects/demo"}], config_path)
    monkeypatch.setattr(reconciler, "RECONCILE_STATE_PATH", state_path)

    reconciler.run_reconcile_once(
        config_path=config_path,
        home=home,
        conflicts_path=tmp_path / "conflicts.json",
        peers=(),
    )

    state = reconciler.read_reconcile_state(state_path)
    last_summary = state["last_summary"]
    assert last_summary["repo_count"] == 0
    assert last_summary["published"] == 1
    assert last_summary["errors"] == 1  # no syncable peers advertised
    assert last_summary["error_details"] == ["no syncable peers advertised"]
    # Flat legacy keys remain for older readers.
    assert state["fast_forwarded"] == 0
    assert state["diverged"] == 0


def test_auth_header_is_resolved_per_repo(tmp_path: Path, monkeypatch) -> None:
    """Long sweeps outlive one access token; each repo must re-resolve it."""
    from openbase_coder_cli import sync_config
    from openbase_coder_cli.code_sync.eligibility import SyncPeer

    home = tmp_path / "home"
    for name in ("alpha", "beta"):
        repo = _init_repo(home / "Projects" / name)
        _commit(repo, "app.py", "print('v1')\n", "initial")
        peer_clone = tmp_path / f"{name}-peer"
        subprocess.run(
            ["git", "clone", "--quiet", str(repo), str(peer_clone)],
            capture_output=True,
            check=True,
        )
    config_path = tmp_path / "sync-config.json"
    sync_config.set_sync_folders([{"relpath": "Projects"}], config_path)
    monkeypatch.setattr(
        reconciler, "RECONCILE_STATE_PATH", tmp_path / "reconcile-state.json"
    )
    token_calls = {"count": 0}

    def counting_token(_self) -> str:
        token_calls["count"] += 1
        return f"token-{token_calls['count']}"

    monkeypatch.setattr(reconciler.TokenManager, "get_access_token", counting_token)
    monkeypatch.setattr(
        reconciler,
        "peer_git_url",
        lambda _peer, _folder_id, relpath: str(tmp_path / f"{Path(relpath).name}-peer"),
    )
    peer = SyncPeer("peer", "peer", "desktop", "peer.test", "engine")

    summary = reconciler.run_reconcile_once(
        config_path=config_path,
        home=home,
        conflicts_path=tmp_path / "conflicts.json",
        peers=(peer,),
    )

    assert summary["errors"] == []
    # One initial probe plus at least one resolution per repo.
    assert token_calls["count"] >= 3
