from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from openbase_coder_cli import sync_config
from openbase_coder_cli.code_sync import reconciler, repositories
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


def _init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)],
        capture_output=True,
        check=True,
    )
    return path


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _copy_without_git(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name == ".git":
            continue
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def _pair(tmp_path: Path) -> tuple[Path, Path]:
    source = _init(tmp_path / "source")
    _commit(source, "app.py", "print('v1')\n", "initial")
    peer = tmp_path / "peer"
    subprocess.run(
        ["git", "clone", "-q", str(source), str(peer)],
        capture_output=True,
        check=True,
    )
    return source, peer


def test_manifest_tracks_branch_head_and_stays_out_of_status(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    head = _commit(repo, "app.py", "print('ok')\n", "initial")
    _git(repo, "remote", "add", "origin", "git@github.com:openbase/example.git")

    manifest = repositories.ensure_repository_manifest(repo)

    assert manifest == {
        "schema_version": 1,
        "branch": "main",
        "head": head,
        "origin_url": "git@github.com:openbase/example.git",
    }
    assert repositories.read_repository_manifest(repo) == manifest
    assert _git(repo, "status", "--porcelain") == ""


def test_manifest_never_serializes_credentialed_origin(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    _commit(repo, "app.py", "print('ok')\n", "initial")
    _git(repo, "remote", "add", "origin", "https://secret@github.com/org/repo.git")

    manifest = repositories.ensure_repository_manifest(repo)

    assert manifest is not None
    assert "origin_url" not in manifest
    assert "secret" not in (repo / repositories.REPO_MANIFEST_NAME).read_text()


def test_synced_manifest_bootstraps_repository_without_git_dir(tmp_path: Path) -> None:
    source = _init(tmp_path / "source")
    head = _commit(source, "app.py", "print('ready')\n", "initial")
    _git(source, "remote", "add", "origin", "git@github.com:openbase/example.git")
    repositories.ensure_repository_manifest(source)
    destination = tmp_path / "destination"
    _copy_without_git(source, destination)

    action = repositories.adopt_repository(destination, remote_urls=(str(source),))

    assert action == "adopted"
    assert (destination / ".git").is_dir()
    assert _git(destination, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(destination, "rev-parse", "HEAD") == head
    assert (
        _git(destination, "remote", "get-url", "origin")
        == "git@github.com:openbase/example.git"
    )
    assert _git(destination, "status", "--porcelain") == ""


def test_discovery_reports_repository_bootstrap_candidate(tmp_path: Path) -> None:
    source = _init(tmp_path / "source")
    _commit(source, "app.py", "print('ready')\n", "initial")
    repositories.ensure_repository_manifest(source)
    destination = tmp_path / "folder" / "repo"
    _copy_without_git(source, destination)

    repos, worktrees, repository_candidates = reconciler.discover_repos_and_candidates(
        tmp_path / "folder"
    )

    assert repos == []
    assert worktrees == []
    assert repository_candidates == [destination]


def test_manifest_enforces_branch_without_rewriting_synced_files(
    tmp_path: Path,
) -> None:
    local, source = _pair(tmp_path)
    _git(source, "checkout", "-q", "-b", "feature")
    head = _commit(source, "feature.py", "feature work\n", "feature")
    manifest = repositories.ensure_repository_manifest(source)
    assert manifest is not None
    _copy_without_git(source, local)

    action = repositories.converge_repository_to_manifest(
        local, manifest, remote_urls=(str(source),)
    )

    assert action == "converged"
    assert _git(local, "rev-parse", "--abbrev-ref", "HEAD") == "feature"
    assert _git(local, "rev-parse", "HEAD") == head
    assert (local / "feature.py").read_text() == "feature work\n"
    assert _git(local, "status", "--porcelain") == ""


def test_divergent_commit_is_preserved_before_manifest_convergence(
    tmp_path: Path,
) -> None:
    local, source = _pair(tmp_path)
    local_head = _commit(local, "app.py", "print('local')\n", "local")
    source_head = _commit(source, "app.py", "print('source')\n", "source")
    manifest = repositories.ensure_repository_manifest(source)
    assert manifest is not None
    _copy_without_git(source, local)

    action = repositories.converge_repository_to_manifest(
        local, manifest, remote_urls=(str(source),)
    )

    assert action.startswith("converged; backup=refs/openbase-code-sync/backups/")
    assert _git(local, "rev-parse", "HEAD") == source_head
    assert local_head in _git(
        local,
        "for-each-ref",
        "--format=%(objectname)",
        "refs/openbase-code-sync/backups/",
    )
    assert _git(local, "status", "--porcelain") == ""


def test_staged_changes_defer_manifest_convergence(tmp_path: Path) -> None:
    local, source = _pair(tmp_path)
    _git(source, "checkout", "-q", "-b", "feature")
    _commit(source, "feature.py", "feature work\n", "feature")
    manifest = repositories.ensure_repository_manifest(source)
    assert manifest is not None
    (local / "staged.txt").write_text("keep staged\n", encoding="utf-8")
    _git(local, "add", "staged.txt")

    action = repositories.converge_repository_to_manifest(
        local, manifest, remote_urls=(str(source),)
    )

    assert action == "staged_changes"
    assert _git(local, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert "A  staged.txt" in _git(local, "status", "--porcelain")


def test_reconcile_tick_consumes_remote_branch_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    local = _init(home / "Projects" / "demo")
    _commit(local, "app.py", "print('v1')\n", "initial")
    source = tmp_path / "source"
    subprocess.run(
        ["git", "clone", "-q", str(local), str(source)],
        capture_output=True,
        check=True,
    )
    config_path = tmp_path / "sync-config.json"
    state_path = tmp_path / "reconcile-state.json"
    conflicts_path = tmp_path / "conflicts.json"
    sync_config.set_sync_folders([{"relpath": "Projects/demo"}], config_path)
    peer = SyncPeer("peer", "peer", "desktop", "peer.test", "engine")
    monkeypatch.setattr(reconciler, "RECONCILE_STATE_PATH", state_path)
    monkeypatch.setattr(
        reconciler.TokenManager, "get_access_token", lambda _self: "token"
    )
    monkeypatch.setattr(reconciler, "peer_git_url", lambda *_args: str(source))

    reconciler.run_reconcile_once(
        config_path=config_path,
        home=home,
        conflicts_path=conflicts_path,
        peers=(peer,),
    )
    _git(source, "checkout", "-q", "-b", "feature")
    source_head = _commit(source, "feature.py", "feature work\n", "feature")
    repositories.ensure_repository_manifest(source)
    _copy_without_git(source, local)

    summary = reconciler.run_reconcile_once(
        config_path=config_path,
        home=home,
        conflicts_path=conflicts_path,
        peers=(peer,),
    )

    assert summary["repository_manifests"] == [{"path": "", "action": "converged"}]
    assert _git(local, "rev-parse", "--abbrev-ref", "HEAD") == "feature"
    assert _git(local, "rev-parse", "HEAD") == source_head


def test_stale_manifest_never_rewinds_a_locally_ahead_branch(tmp_path: Path) -> None:
    # The 2026-08-25 incident: the peer's manifest lagged this machine and
    # convergence rewound real commits into a backup ref. A manifest whose
    # head is an ancestor of the local head must lose to local history.
    local, source = _pair(tmp_path)
    manifest = repositories.ensure_repository_manifest(source)
    assert manifest is not None
    ahead_head = _commit(local, "app.py", "print('newer local work')\n", "ahead")

    action = repositories.converge_repository_to_manifest(
        local, manifest, remote_urls=(str(source),)
    )

    assert action == "local_ahead"
    assert _git(local, "rev-parse", "HEAD") == ahead_head


def test_divergence_displacement_records_a_branch_conflict(tmp_path: Path) -> None:
    from openbase_coder_cli.code_sync.conflicts import read_conflicts

    local, source = _pair(tmp_path)
    local_head = _commit(local, "app.py", "print('local')\n", "local")
    source_head = _commit(source, "app.py", "print('source')\n", "source")
    manifest = repositories.ensure_repository_manifest(source)
    assert manifest is not None
    _copy_without_git(source, local)
    conflicts_path = tmp_path / "conflicts.json"

    action = repositories.converge_repository_to_manifest(
        local,
        manifest,
        remote_urls=(str(source),),
        folder_id="projects",
        repo_relpath="repo",
        conflicts_path=conflicts_path,
    )

    assert action.startswith("converged; backup=")
    conflicts = read_conflicts(conflicts_path)
    assert any(
        entry.get("local_sha") == local_head and entry.get("remote_sha") == source_head
        for entry in conflicts
    )
