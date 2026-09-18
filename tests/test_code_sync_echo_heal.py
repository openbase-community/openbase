"""Echo-heal: fast-forward trunk repos dirtied purely by sync echoes.

Machine A commits+pushes; Syncthing echoes the file changes to machine B
whose HEAD is behind, so B's tree looks dirty with "someone's uncommitted
work". The heal must prove byte-identity against origin before touching
anything, and any real local change must abort it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from openbase_coder_cli.code_sync import echo_heal

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


def _make_upstream(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)], capture_output=True, check=True
    )
    (path / "a.txt").write_text("a v1\n", encoding="utf-8")
    (path / "b.txt").write_text("b v1\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "initial")
    return path


def _clone(upstream: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(dest)],
        capture_output=True,
        check=True,
    )
    return dest


def _commit_upstream(upstream: Path, filename: str, content: str | None) -> str:
    """Commit a modification (or deletion when content is None) upstream."""
    target = upstream / filename
    if content is None:
        _git(upstream, "rm", "-q", filename)
    else:
        target.write_text(content, encoding="utf-8")
        _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", f"update {filename}")
    return _git(upstream, "rev-parse", "HEAD")


def _echo(local: Path, filename: str, content: str | None) -> None:
    """Simulate Syncthing delivering the upstream file state."""
    target = local / filename
    if content is None:
        target.unlink()
    else:
        target.write_text(content, encoding="utf-8")


def _heal(local: Path, **kwargs) -> dict:
    kwargs.setdefault("min_fetch_interval", 0)
    return echo_heal.heal_repo_echo(local, **kwargs)


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    upstream = _make_upstream(tmp_path / "upstream")
    local = _clone(upstream, tmp_path / "local")
    return upstream, local


def test_pure_echo_is_healed(tmp_path: Path) -> None:
    upstream, local = _setup(tmp_path)
    new_head = _commit_upstream(upstream, "a.txt", "a v2\n")
    _echo(local, "a.txt", "a v2\n")

    result = _heal(local)

    assert result["action"] == echo_heal.ACTION_HEALED
    assert result["branch"] == "main"
    assert _git(local, "rev-parse", "HEAD") == new_head
    assert _git(local, "status", "--porcelain") == ""


def test_any_differing_file_aborts_and_preserves_work(tmp_path: Path) -> None:
    upstream, local = _setup(tmp_path)
    _commit_upstream(upstream, "a.txt", "a v2\n")
    _echo(local, "a.txt", "a v2\n")
    (local / "b.txt").write_text("someone's real in-flight work\n", encoding="utf-8")
    head_before = _git(local, "rev-parse", "HEAD")

    result = _heal(local)

    assert result["action"] == echo_heal.ACTION_NOT_ECHO
    assert "b.txt" in result["detail"]
    assert _git(local, "rev-parse", "HEAD") == head_before
    assert (local / "b.txt").read_text(encoding="utf-8") == (
        "someone's real in-flight work\n"
    )
    assert (local / "a.txt").read_text(encoding="utf-8") == "a v2\n"


def test_staged_changes_abort(tmp_path: Path) -> None:
    upstream, local = _setup(tmp_path)
    _commit_upstream(upstream, "a.txt", "a v2\n")
    _echo(local, "a.txt", "a v2\n")
    (local / "staged.txt").write_text("mid-change\n", encoding="utf-8")
    _git(local, "add", "staged.txt")
    head_before = _git(local, "rev-parse", "HEAD")

    result = _heal(local)

    assert result["action"] == echo_heal.ACTION_SKIPPED_STAGED
    assert _git(local, "rev-parse", "HEAD") == head_before


def test_unrelated_untracked_files_are_left_alone(tmp_path: Path) -> None:
    upstream, local = _setup(tmp_path)
    _commit_upstream(upstream, "a.txt", "a v2\n")
    _echo(local, "a.txt", "a v2\n")
    (local / "scratch.txt").write_text("agent scratch\n", encoding="utf-8")

    result = _heal(local)

    assert result["action"] == echo_heal.ACTION_HEALED
    assert (local / "scratch.txt").read_text(encoding="utf-8") == "agent scratch\n"
    assert "scratch.txt" in _git(local, "status", "--porcelain")


def test_untracked_echo_of_added_file_is_adopted(tmp_path: Path) -> None:
    upstream, local = _setup(tmp_path)
    (upstream / "a.txt").write_text("a v2\n", encoding="utf-8")
    (upstream / "new.txt").write_text("brand new\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "modify a.txt, add new.txt")
    new_head = _git(upstream, "rev-parse", "HEAD")
    _echo(local, "a.txt", "a v2\n")
    _echo(local, "new.txt", "brand new\n")  # Arrives as an untracked file.

    result = _heal(local)

    assert result["action"] == echo_heal.ACTION_HEALED
    assert _git(local, "rev-parse", "HEAD") == new_head
    assert _git(local, "status", "--porcelain") == ""


def test_untracked_only_dirt_is_not_healed(tmp_path: Path) -> None:
    # An adds-only echo leaves no tracked file dirty; the origin-based heal
    # deliberately stays out (the peer reconciler's fast-forward path
    # already covers a worktree whose only dirt is identical new files).
    upstream, local = _setup(tmp_path)
    (upstream / "new.txt").write_text("brand new\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "add new.txt")
    _echo(local, "new.txt", "brand new\n")
    head_before = _git(local, "rev-parse", "HEAD")

    result = _heal(local)

    assert result["action"] == "untracked_only"
    assert result["action"] in echo_heal.SILENT_ACTIONS
    assert _git(local, "rev-parse", "HEAD") == head_before


def test_untracked_file_colliding_with_origin_aborts(tmp_path: Path) -> None:
    upstream, local = _setup(tmp_path)
    (upstream / "a.txt").write_text("a v2\n", encoding="utf-8")
    (upstream / "new.txt").write_text("origin version\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "modify a.txt, add new.txt")
    _echo(local, "a.txt", "a v2\n")
    (local / "new.txt").write_text("local draft with same name\n", encoding="utf-8")
    head_before = _git(local, "rev-parse", "HEAD")

    result = _heal(local)

    assert result["action"] == echo_heal.ACTION_NOT_ECHO
    assert "new.txt" in result["detail"]
    assert "untracked" in result["detail"]
    assert _git(local, "rev-parse", "HEAD") == head_before
    assert (local / "new.txt").read_text(encoding="utf-8") == (
        "local draft with same name\n"
    )


def test_deletion_echo_is_healed(tmp_path: Path) -> None:
    upstream, local = _setup(tmp_path)
    new_head = _commit_upstream(upstream, "b.txt", None)
    _echo(local, "b.txt", None)

    result = _heal(local)

    assert result["action"] == echo_heal.ACTION_HEALED
    assert _git(local, "rev-parse", "HEAD") == new_head
    assert not (local / "b.txt").exists()
    assert _git(local, "status", "--porcelain") == ""


def test_deletion_not_matching_origin_aborts(tmp_path: Path) -> None:
    upstream, local = _setup(tmp_path)
    _commit_upstream(upstream, "a.txt", "a v2\n")
    _echo(local, "a.txt", "a v2\n")
    (local / "b.txt").unlink()  # Locally deleted, still present upstream.

    result = _heal(local)

    assert result["action"] == echo_heal.ACTION_NOT_ECHO
    assert "b.txt" in result["detail"]


def test_dirty_but_not_behind_is_silent(tmp_path: Path) -> None:
    upstream, local = _setup(tmp_path)
    (local / "a.txt").write_text("local edit\n", encoding="utf-8")

    result = _heal(local)

    assert result["action"] == "not_behind"
    assert result["action"] in echo_heal.SILENT_ACTIONS
    assert (local / "a.txt").read_text(encoding="utf-8") == "local edit\n"


def test_lockfile_deletions_classified_without_fetching(tmp_path: Path) -> None:
    upstream = _make_upstream(tmp_path / "upstream")
    (upstream / "uv.lock").write_text("lock v1\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "add lockfile")
    local = _clone(upstream, tmp_path / "local")
    (local / "uv.lock").unlink()  # The known symlink shadow-regen artifact.
    head_before = _git(local, "rev-parse", "HEAD")

    result = _heal(local, allow_fetch=False)

    assert result["action"] == echo_heal.ACTION_LOCKFILE_ARTIFACT
    assert "uv.lock" in result["detail"]
    assert "known" in result["detail"]
    assert _git(local, "rev-parse", "HEAD") == head_before


def test_lockfile_artifact_mixed_with_echo_blocks_with_classified_detail(
    tmp_path: Path,
) -> None:
    upstream = _make_upstream(tmp_path / "upstream")
    (upstream / "uv.lock").write_text("lock v1\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "add lockfile")
    local = _clone(upstream, tmp_path / "local")
    _commit_upstream(upstream, "a.txt", "a v2\n")
    _echo(local, "a.txt", "a v2\n")
    (local / "uv.lock").unlink()

    result = _heal(local)

    assert result["action"] == echo_heal.ACTION_NOT_ECHO
    assert "uv.lock" in result["detail"]
    assert "known lockfile sync artifact" in result["detail"]


def test_detection_only_mode_reports_but_does_not_touch(tmp_path: Path) -> None:
    upstream, local = _setup(tmp_path)
    _commit_upstream(upstream, "a.txt", "a v2\n")
    _echo(local, "a.txt", "a v2\n")
    head_before = _git(local, "rev-parse", "HEAD")

    result = _heal(local, apply=False)

    assert result["action"] == echo_heal.ACTION_ECHO_DETECTED
    assert "safe to fast-forward" in result["detail"]
    assert _git(local, "rev-parse", "HEAD") == head_before
    assert "a.txt" in _git(local, "status", "--porcelain")


def test_no_fetch_uses_cached_tracking_ref(tmp_path: Path) -> None:
    upstream, local = _setup(tmp_path)
    _commit_upstream(upstream, "a.txt", "a v2\n")
    _echo(local, "a.txt", "a v2\n")

    # Cached origin/main is still the clone-time tip: not provably behind.
    stale = _heal(local, allow_fetch=False)
    assert stale["action"] == "not_behind"

    _git(local, "fetch", "-q", "origin")
    healed = _heal(local, allow_fetch=False)
    assert healed["action"] == echo_heal.ACTION_HEALED
    assert "cached origin tracking ref" in healed["detail"]


def test_detached_head_and_missing_origin_are_silent(tmp_path: Path) -> None:
    upstream, local = _setup(tmp_path)
    _git(local, "checkout", "-q", "--detach")
    assert _heal(local)["action"] == "skipped_detached"
    _git(local, "checkout", "-q", "main")

    solo = _make_upstream(tmp_path / "solo")  # No origin remote at all.
    (solo / "a.txt").write_text("dirty\n", encoding="utf-8")
    result = _heal(solo)
    assert result["action"] == "no_origin_branch"
    assert result["action"] in echo_heal.SILENT_ACTIONS


def test_fetch_rate_limit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    assert echo_heal._fetch_due(repo, 300, now=1000.0) is True
    assert echo_heal._fetch_due(repo, 300, now=1100.0) is False
    assert echo_heal._fetch_due(repo, 300, now=1301.0) is True
    # A different repo has its own clock.
    assert echo_heal._fetch_due(tmp_path / "other", 300, now=1100.0) is True


def test_reconcile_tick_heals_and_respects_kill_switch(
    tmp_path: Path, monkeypatch
) -> None:
    from openbase_coder_cli import sync_config
    from openbase_coder_cli.code_sync import reconciler

    home = tmp_path / "home"
    upstream = _make_upstream(tmp_path / "upstream")
    local = _clone(upstream, home / "code" / "app")
    new_head = _commit_upstream(upstream, "a.txt", "a v2\n")
    _echo(local, "a.txt", "a v2\n")

    config_path = tmp_path / "sync-config.json"
    sync_config.set_sync_folders([{"relpath": "code"}], config_path)
    monkeypatch.setattr(
        reconciler, "RECONCILE_STATE_PATH", tmp_path / "reconcile-state.json"
    )
    echo_heal._last_fetch_attempts.clear()

    monkeypatch.setenv("OPENBASE_CODE_SYNC_ECHO_HEAL", "0")
    summary = reconciler.run_reconcile_once(
        config_path=config_path,
        home=home,
        conflicts_path=tmp_path / "conflicts.json",
        peers=(),
    )
    (entry,) = summary["echo_heals"]
    assert entry["action"] == echo_heal.ACTION_ECHO_DETECTED
    assert entry["path"] == "app"
    assert _git(local, "rev-parse", "HEAD") != new_head

    monkeypatch.delenv("OPENBASE_CODE_SYNC_ECHO_HEAL")
    echo_heal._last_fetch_attempts.clear()
    summary = reconciler.run_reconcile_once(
        config_path=config_path,
        home=home,
        conflicts_path=tmp_path / "conflicts.json",
        peers=(),
    )
    (entry,) = summary["echo_heals"]
    assert entry["action"] == echo_heal.ACTION_HEALED
    assert _git(local, "rev-parse", "HEAD") == new_head
    assert reconciler.reconcile_counts(summary)["echo_healed"] == 1
