"""The Sync page's view of the daemon: health, backlog, conflicts, stale locks."""

from __future__ import annotations

# ruff: noqa: E402, I001

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django

django.setup()

import pytest
from django.urls import resolve as resolve_url
from rest_framework.test import APIRequestFactory, force_authenticate

from openbase_coder_cli import sync_daemon, sync_state
from openbase_coder_cli.openbase_coder_cli_app import sync_daemon_api

A_HASH = "a" * 64
B_HASH = "b" * 64
ANCESTOR = "c" * 64


def _request(method: str, path: str, data: dict | None = None):
    factory = APIRequestFactory()
    fn = {"GET": factory.get, "POST": factory.post}[method]
    request = fn(path, data=data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def _status(**overrides):
    status = {
        "device": "laptop",
        "role": "edge",
        "uptime_s": 10,
        "roots": [
            {
                "id": "projects",
                "path": "/Users/x/Projects",
                "entries": 800,
                "seq": 1000,
                "pending_fetches": 2,
                "scanning": False,
            },
            {
                "id": "skills",
                "path": "/Users/x/.agents/skills",
                "entries": 20,
                "seq": 30,
                "pending_fetches": 0,
                "scanning": False,
            },
        ],
        "peers": [
            {
                "device": "mini",
                "role": "hub",
                "connected_at": "2026-10-07T20:00:00-04:00",
                "rtt_ms": 40.0,
                "roots": {
                    "projects": {
                        "sent_seq": 900,
                        "acked_seq": 850,
                        "applied_peer_seq": 700,
                    },
                    "skills": {"sent_seq": 30, "acked_seq": 30, "applied_peer_seq": 9},
                },
            }
        ],
        "open_conflicts": 3,
    }
    status.update(overrides)
    return status


# --- overview ---------------------------------------------------------------


def test_overview_backlog_per_root_and_peer():
    result = sync_state.overview(
        _status(),
        config_roots=[
            {"id": "projects", "path": "~/Projects", "pins": ["big"], "ignore": ["/d"]}
        ],
        stale_lock_count=2,
    )

    assert result["state"] == "syncing"
    projects = result["roots"][0]
    assert projects["unsent"] == 100 and projects["unacked"] == 50
    assert projects["peers"][0] == {
        "device": "mini",
        "sent_seq": 900,
        "acked_seq": 850,
        "received_seq": 700,
        "unsent": 100,
        "unacked": 50,
    }
    assert projects["pins"] == ["big"] and projects["ignore"] == ["/d"]
    assert result["roots"][1]["unsent"] == 0
    assert result["totals"] == {
        "unsent": 100,
        "unacked": 50,
        "pending_fetches": 2,
        "entries": 820,
    }
    assert result["attention"] == {
        "conflicts": 3,
        "stale_locks": 2,
        "low_disk": [],
        "held_deletes": [],
        "needed": True,
    }
    assert projects["disk"] is None and result["versions"] is None


def test_overview_states():
    caught_up = _status(open_conflicts=0)
    caught_up["roots"][0]["pending_fetches"] = 0
    for root in caught_up["peers"][0]["roots"].values():
        root["sent_seq"] = root["acked_seq"] = 1000
    caught_up["roots"][0]["seq"] = 1000
    caught_up["roots"][1]["seq"] = 1000
    in_sync = sync_state.overview(caught_up, stale_lock_count=None)
    assert in_sync["state"] == "in_sync"
    assert in_sync["attention"] == {
        "conflicts": 0,
        "stale_locks": None,
        "low_disk": [],
        "held_deletes": [],
        "needed": False,
    }

    scanning = _status()
    scanning["roots"][1]["scanning"] = True
    assert sync_state.overview(scanning)["state"] == "scanning"

    assert sync_state.overview(_status(peers=None))["state"] == "offline"
    assert sync_state.overview(_status(role="hub", peers=[]))["state"] == "waiting"


def test_overview_skips_peers_that_do_not_sync_a_folder():
    """A hub next to a project-only computer: that computer syncs one folder,
    so the others show no backlog for it (it never receives them)."""
    status = _status(role="hub")
    status["peers"].append(
        {
            "device": "cloud",
            "role": "edge",
            "roots": {"skills": {"sent_seq": 30, "acked_seq": 30}},
        }
    )
    status["roots"][0]["seq"] = 900
    status["peers"][0]["roots"]["projects"]["acked_seq"] = 900
    status["roots"][0]["pending_fetches"] = 0

    result = sync_state.overview(status)

    projects, skills = result["roots"]
    assert [row["device"] for row in projects["peers"]] == ["mini"]
    assert [row["device"] for row in skills["peers"]] == ["mini", "cloud"]
    assert projects["unsent"] == 0 and result["state"] == "in_sync"


def test_overview_reports_disk_and_low_disk_attention():
    status = _status(open_conflicts=0)
    status["roots"][0]["bytes"] = 123
    status["roots"][0]["disk"] = {
        "free_bytes": 400,
        "total_bytes": 5000,
        "low_water_bytes": 500,
        "low_water_auto": True,
        "below_low_water": True,
        "held_files": 3,
        "held_bytes": 90,
        "refused_writes": 3,
        "lazy_threshold_bytes": 50,
        "pinned_threshold_bytes": 250,
    }
    status["roots"][1]["disk"] = {"free_bytes": -1, "total_bytes": -1}
    status["versions"] = {
        "usage_bytes": 10,
        "quota_bytes": 750,
        "quota_auto": True,
        "retention_days": 30,
        "over_quota": False,
    }
    status["placement"] = {"anchor": "edge", "thin": True}

    result = sync_state.overview(status)

    projects, skills = result["roots"]
    assert projects["only"] == [] and skills["only"] == []
    assert projects["bytes"] == 123
    assert projects["disk"]["below_low_water"] is True
    assert projects["disk"]["held_files"] == 3
    assert skills["disk"]["free_bytes"] is None
    assert result["attention"]["low_disk"] == [projects["path"]]
    assert result["attention"]["needed"] is True
    assert result["versions"]["quota_bytes"] == 750
    assert result["thin"] is True


def test_presence_remembers_peers_that_went_away():
    presence = sync_state.PeerPresence()
    presence.observe([{"device": "mini", "role": "hub"}], 1_790_000_000.0)

    assert presence.offline(["mini"]) == []
    offline = presence.offline([])
    assert offline == [
        {"device": "mini", "role": "hub", "last_seen": "2026-09-21T14:13:20Z"}
    ]


# --- conflicts --------------------------------------------------------------


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "Projects"
    (root / "app" / ".git").mkdir(parents=True)
    (root / "app" / "src").mkdir()
    (root / "notes").mkdir()
    return root


def test_enrich_groups_by_repository_or_folder(tmp_path):
    root = _tree(tmp_path)
    conflicts = [
        {
            "id": 1,
            "root": "projects",
            "path": "app/src/main.py",
            "kind": "content",
            "a_device": "laptop",
            "b_device": "mini",
        },
        {
            "id": 2,
            "root": "projects",
            "path": "app:refs/heads/develop",
            "kind": "git-branch",
            "a_device": "laptop",
            "b_device": "mini",
        },
        {
            "id": 3,
            "root": "projects",
            "path": "notes/today.md",
            "kind": "content",
            "a_device": "mini",
            "b_device": "laptop",
        },
        {
            "id": 4,
            "root": "projects",
            "path": ".:refs/heads/main",
            "kind": "git-branch",
        },
    ]

    enriched = sync_state.enrich_conflicts(
        conflicts,
        roots=[{"id": "projects", "path": str(root)}],
        local_device="laptop",
    )

    by_id = {conflict["id"]: conflict for conflict in enriched}
    assert by_id[1]["repo"] == "app" and by_id[1]["group"] == "app"
    assert by_id[1]["root_path"] == str(root) and by_id[1]["a_is_local"] is True
    assert by_id[2]["repo"] == "app" and by_id[2]["ref"] == "refs/heads/develop"
    assert by_id[3]["repo"] == "" and by_id[3]["group"] == "notes"
    assert by_id[3]["a_is_local"] is False
    assert by_id[4]["repo"] == "" and by_id[4]["ref"] == "refs/heads/main"


def _store(tmp_path: Path, contents: dict[str, bytes]) -> Path:
    store = tmp_path / "versions"
    for content_hash, data in contents.items():
        (store / content_hash[:2]).mkdir(parents=True, exist_ok=True)
        (store / content_hash[:2] / content_hash).write_bytes(data)
    return store


def test_file_conflict_detail_reads_versions_and_diffs(tmp_path):
    store = _store(
        tmp_path,
        {A_HASH: b"one\ntwo mine\n", B_HASH: b"one\ntwo theirs\n", ANCESTOR: b"\0bin"},
    )
    root = _tree(tmp_path)
    (root / "app" / "src" / "main.py").write_text("one\ntwo mine\n")
    conflict = {
        "kind": "content",
        "path": "app/src/main.py",
        "a_hash": A_HASH,
        "b_hash": B_HASH,
        "ancestor": ANCESTOR,
        "a_device": "laptop",
        "b_device": "mini",
    }

    detail = sync_state.file_conflict_detail(conflict, store=store, root_path=root)

    assert detail["versions"]["a"]["text"] == "one\ntwo mine\n"
    assert detail["versions"]["b"]["size"] == len(b"one\ntwo theirs\n")
    assert detail["versions"]["ancestor"]["binary"] is True
    assert detail["versions"]["ancestor"]["text"] is None
    assert "-two theirs" in detail["diff"] and "+two mine" in detail["diff"]
    assert detail["current"]["exists"] is True


def test_file_conflict_detail_missing_and_bad_hashes(tmp_path):
    store = _store(tmp_path, {})
    detail = sync_state.file_conflict_detail(
        {"kind": "delete-edit", "path": "x", "a_hash": "", "b_hash": "../../etc"},
        store=store,
        root_path=None,
    )

    assert detail["versions"]["a"]["available"] is False
    assert detail["versions"]["b"]["available"] is False
    assert detail["diff"] is None and detail["current"] is None
    assert sync_state.version_path("../" + "a" * 61, store) is None


def test_large_versions_are_not_inlined(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_state, "MAX_TEXT_BYTES", 4)
    store = _store(tmp_path, {A_HASH: b"0123456789"})

    info = sync_state.describe_version(A_HASH, store)

    assert info == {
        "hash": A_HASH,
        "available": True,
        "size": 10,
        "binary": False,
        "truncated": True,
        "text": None,
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    ).stdout.strip()


def test_git_branch_detail_reports_both_sides(tmp_path):
    root = tmp_path / "Projects"
    repo = root / "app"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "mine")
    mine = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "other", base)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "theirs 1")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "theirs 2")
    theirs = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    detail = sync_state.git_branch_detail(
        {"path": "app:refs/heads/main", "a_hash": mine, "b_hash": theirs},
        root_path=root,
    )

    assert detail["branch"] == "main" and detail["checked_out"] is True
    assert detail["current_sha"] == mine and detail["moved_since"] is False
    assert detail["other_available"] is True and detail["merge_base"] == base
    assert (detail["this_ahead"], detail["other_ahead"]) == (1, 2)
    assert [c["subject"] for c in detail["other_only"]] == ["theirs 2", "theirs 1"]
    assert [c["subject"] for c in detail["this_only"]] == ["mine"]

    reversed_sides = sync_state.git_branch_detail(
        {
            "path": "app:refs/heads/main",
            "a_hash": theirs,
            "b_hash": mine,
            "a_device": "mini",
            "b_device": "laptop",
        },
        root_path=root,
        local_device="laptop",
    )
    assert reversed_sides["this_sha"] == mine
    assert reversed_sides["other_sha"] == theirs
    assert reversed_sides["current_sha"] == mine
    assert (reversed_sides["this_ahead"], reversed_sides["other_ahead"]) == (1, 2)

    _git(repo, "commit", "-q", "--allow-empty", "-m", "later")
    moved = sync_state.git_branch_detail(
        {"path": "app:refs/heads/main", "a_hash": mine, "b_hash": "f" * 40},
        root_path=root,
    )
    assert moved["moved_since"] is True and moved["other_available"] is False


# --- stale locks ------------------------------------------------------------


def test_stale_lock_cache_refreshes_in_background():
    calls = []

    def fetch():
        calls.append(1)
        return {"/r": ["/r/.git/index.lock"]}

    now = [1000.0]
    cache = sync_state.StaleLockCache(fetch=fetch, ttl_s=60, clock=lambda: now[0])

    first = cache.snapshot()
    assert first["locks"] is None and first["refreshing"] is True
    cache.wait(5)
    second = cache.snapshot()
    assert second["locks"] == {"/r": ["/r/.git/index.lock"]}
    assert second["refreshing"] is False and len(calls) == 1
    assert cache.known("/r/.git/index.lock")

    now[0] += 61
    cache.snapshot()
    cache.wait(5)
    assert len(calls) == 2
    cache.forget("/r/.git/index.lock")
    assert cache.snapshot()["locks"] == {}


def test_stale_lock_cache_keeps_last_result_on_error():
    results = [{"/r": ["/r/.git/HEAD.lock"]}, RuntimeError("daemon busy")]

    def fetch():
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    cache = sync_state.StaleLockCache(fetch=fetch, ttl_s=0)
    cache.snapshot()
    cache.wait(5)
    time.sleep(0.01)
    cache.snapshot()
    cache.wait(5)
    snapshot = cache.snapshot(refresh=False)
    assert snapshot["locks"] == {"/r": ["/r/.git/HEAD.lock"]}
    assert snapshot["error"] == "daemon busy"


def _lock(tmp_path: Path, age_s: float, name: str = "index.lock") -> Path:
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    lock = git_dir / name
    lock.write_text("")
    then = time.time() - age_s
    os.utime(lock, (then, then))
    return lock


def test_move_lock_to_trash_moves_old_unheld_locks(tmp_path):
    lock = _lock(tmp_path, 3600)
    trash = tmp_path / "trash"

    result = sync_state.move_lock_to_trash(
        str(lock), known=lambda path: True, holders=lambda path: [], trash=trash
    )

    assert not lock.exists()
    moved = Path(result["to"])
    assert moved.is_file() and trash in moved.parents


@pytest.mark.parametrize(
    ("age_s", "holders", "known", "status", "message"),
    [
        (3600, [], False, 404, "not reported"),
        (60, [], True, 409, "less than 10 minutes"),
        (3600, [4242], True, 409, "pid 4242"),
        (3600, None, True, 409, "lsof"),
    ],
)
def test_move_lock_to_trash_refusals(tmp_path, age_s, holders, known, status, message):
    lock = _lock(tmp_path, age_s)

    with pytest.raises(sync_state.LockMoveError) as caught:
        sync_state.move_lock_to_trash(
            str(lock),
            known=lambda path: known,
            holders=lambda path: holders,
            trash=tmp_path / "trash",
        )

    assert caught.value.status == status and message in str(caught.value)
    assert lock.exists()


def test_move_lock_to_trash_only_moves_git_locks(tmp_path):
    other = tmp_path / "notes.lock"
    other.write_text("")
    with pytest.raises(sync_state.LockMoveError, match="not a git lock"):
        sync_state.move_lock_to_trash(
            str(other), known=lambda path: True, holders=lambda path: []
        )
    with pytest.raises(sync_state.LockMoveError, match="already gone"):
        sync_state.move_lock_to_trash(
            str(tmp_path / "repo" / ".git" / "index.lock"),
            known=lambda path: True,
            holders=lambda path: [],
        )
    ref_lock = tmp_path / "repo" / ".git" / "refs" / "heads" / "main.lock"
    ref_lock.parent.mkdir(parents=True)
    ref_lock.write_text("")
    assert sync_state._is_git_lock(ref_lock)


def test_describe_stale_locks_reports_age(tmp_path):
    lock = _lock(tmp_path, 1200)

    rows = sync_state.describe_stale_locks(
        {str(tmp_path / "repo"): [str(lock), str(tmp_path / "gone.lock")]}
    )

    assert rows[0]["exists"] is True and 1190 <= rows[0]["age_s"] <= 1300
    assert rows[1]["exists"] is False and rows[1]["age_s"] is None
    assert sync_state.describe_stale_locks(None) is None


# --- API --------------------------------------------------------------------


class FakeClient:
    resolved: list[tuple[int, str]] = []
    fail_resolve_after: int | None = None
    conflicts_data: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def status(self):
        return _status()

    def metrics(self):
        return {"local_changes": 1}

    def conflicts(self, root=None):
        return FakeClient.conflicts_data

    def resolve(self, conflict_id, choice):
        if (
            FakeClient.fail_resolve_after is not None
            and len(FakeClient.resolved) >= FakeClient.fail_resolve_after
        ):
            raise sync_daemon.SyncDaemonError("sync daemon unreachable")
        FakeClient.resolved.append((conflict_id, choice))


@pytest.fixture
def api(monkeypatch, tmp_path):
    root = _tree(tmp_path)
    FakeClient.resolved = []
    FakeClient.fail_resolve_after = None
    FakeClient.conflicts_data = [
        {
            "id": 1,
            "root": "projects",
            "path": "app/src/main.py",
            "kind": "content",
            "a_hash": A_HASH,
            "b_hash": B_HASH,
            "ancestor": "",
            "a_device": "laptop",
            "b_device": "mini",
            "created_ns": 1,
            "label": "",
        },
        {
            "id": 2,
            "root": "projects",
            "path": "app:refs/heads/develop",
            "kind": "git-branch",
            "a_hash": "1" * 40,
            "b_hash": "2" * 40,
            "ancestor": "",
            "a_device": "laptop",
            "b_device": "mini",
            "created_ns": 2,
            "label": "",
        },
        {
            "id": 3,
            "root": "projects",
            "path": "notes/a.md",
            "kind": "delete-edit",
            "a_hash": "",
            "b_hash": B_HASH,
            "ancestor": "",
            "a_device": "mini",
            "b_device": "laptop",
            "created_ns": 3,
            "label": "",
        },
    ]
    config = tmp_path / "sync" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        f'device_id = "laptop"\nrole = "edge"\nstate_dir = "{tmp_path / "sync"}"\n'
        f'\n[[roots]]\nid = "projects"\npath = "{root}"\n'
    )
    _store(tmp_path / "sync", {A_HASH: b"mine\n", B_HASH: b"theirs\n"})
    monkeypatch.setattr(sync_daemon, "SYNC_DAEMON_CONFIG_PATH", config)
    monkeypatch.setattr(sync_daemon, "SyncDaemonClient", FakeClient)
    sync_state.PRESENCE.reset()
    cache = sync_state.StaleLockCache(fetch=lambda: {"/r": ["/r/.git/index.lock"]})
    monkeypatch.setattr(sync_state, "STALE_LOCKS", cache)
    return SimpleNamespace(root=root, cache=cache, tmp=tmp_path)


def test_routes():
    assert (
        resolve_url("/api/sync/daemon/conflicts/12/").url_name
        == "sync-daemon-conflict-detail"
    )
    assert (
        resolve_url("/api/sync/daemon/stale-locks/").url_name
        == "sync-daemon-stale-locks"
    )
    assert (
        resolve_url("/api/sync/daemon/stale-locks/trash/").url_name
        == "sync-daemon-stale-lock-trash"
    )


def test_status_includes_overview(api):
    response = sync_daemon_api.sync_daemon_status(
        _request("GET", "/api/sync/daemon/status/")
    )

    assert response.status_code == 200
    overview = response.data["overview"]
    assert overview["state"] == "syncing"
    assert overview["totals"]["unsent"] == 100
    # raw daemon fields stay for older clients
    assert response.data["open_conflicts"] == 3 and response.data["peers"]


def test_conflicts_are_enriched(api):
    response = sync_daemon_api.sync_daemon_conflicts(
        _request("GET", "/api/sync/daemon/conflicts/")
    )

    assert response.data["unresolved_count"] == 3
    first = response.data["conflicts"][0]
    assert first["group"] == "app" and first["a_is_local"] is True


def test_conflict_detail(api):
    response = sync_daemon_api.sync_daemon_conflict_detail(
        _request("GET", "/api/sync/daemon/conflicts/1/"), conflict_id=1
    )
    assert response.status_code == 200
    assert response.data["detail"]["versions"]["a"]["text"] == "mine\n"
    assert response.data["detail"]["versions"]["b"]["text"] == "theirs\n"
    assert "+mine" in response.data["detail"]["diff"]

    branch = sync_daemon_api.sync_daemon_conflict_detail(
        _request("GET", "/api/sync/daemon/conflicts/2/"), conflict_id=2
    )
    assert branch.data["detail"]["kind"] == "git-branch"
    assert branch.data["detail"]["branch"] == "develop"

    gone = sync_daemon_api.sync_daemon_conflict_detail(
        _request("GET", "/api/sync/daemon/conflicts/99/"), conflict_id=99
    )
    assert gone.status_code == 404


def test_bulk_resolve_reports_each_conflict(api):
    response = sync_daemon_api.sync_daemon_conflicts_resolve(
        _request(
            "POST",
            "/api/sync/daemon/conflicts/resolve/",
            {"ids": [1, 2, 3, 99], "action": "keep_local"},
        )
    )

    assert response.status_code == 200
    assert response.data["resolved"] == 2 and response.data["failed"] == 2
    assert FakeClient.resolved == [(1, "a"), (3, "b")]
    errors = {row["id"]: row["error"] for row in response.data["results"]}
    assert "neither" in errors[2] and "no longer open" in errors[99]


def test_bulk_resolve_stops_when_the_daemon_stops_answering(api):
    FakeClient.fail_resolve_after = 1

    response = sync_daemon_api.sync_daemon_conflicts_resolve(
        _request(
            "POST",
            "/api/sync/daemon/conflicts/resolve/",
            {"ids": [1, 3], "action": "use_remote"},
        )
    )

    assert response.data["resolved"] == 1
    assert response.data["results"][1]["error"] == "sync daemon unreachable"


def test_bulk_resolve_limits(api):
    too_many = sync_daemon_api.sync_daemon_conflicts_resolve(
        _request(
            "POST",
            "/api/sync/daemon/conflicts/resolve/",
            {"ids": list(range(sync_daemon_api.MAX_BULK_RESOLVE + 1)), "action": "b"},
        )
    )
    assert too_many.status_code == 400
    empty = sync_daemon_api.sync_daemon_conflicts_resolve(
        _request(
            "POST", "/api/sync/daemon/conflicts/resolve/", {"ids": [], "action": "b"}
        )
    )
    assert empty.status_code == 400


def test_single_resolve_of_closed_conflict_is_404(api):
    response = sync_daemon_api.sync_daemon_conflicts_resolve(
        _request(
            "POST",
            "/api/sync/daemon/conflicts/resolve/",
            {"id": 99, "action": "keep_local"},
        )
    )
    assert response.status_code == 404


def test_stale_lock_endpoints(api, monkeypatch):
    first = sync_daemon_api.sync_daemon_stale_locks(
        _request("GET", "/api/sync/daemon/stale-locks/")
    )
    assert first.data["locks"] is None and first.data["stale_after_s"] == 600
    api.cache.wait(5)
    second = sync_daemon_api.sync_daemon_stale_locks(
        _request("GET", "/api/sync/daemon/stale-locks/")
    )
    assert second.data["locks"][0]["path"] == "/r/.git/index.lock"

    refused = sync_daemon_api.sync_daemon_stale_lock_trash(
        _request("POST", "/api/sync/daemon/stale-locks/trash/", {"path": "/etc/hosts"})
    )
    assert refused.status_code == 404

    lock = _lock(api.tmp, 3600)
    api.cache.locks = {str(lock.parent.parent): [str(lock)]}
    monkeypatch.setattr(sync_state, "lock_holders", lambda path: [])
    monkeypatch.setattr(sync_state, "trash_dir", lambda: api.tmp / "trash")
    moved = sync_daemon_api.sync_daemon_stale_lock_trash(
        _request("POST", "/api/sync/daemon/stale-locks/trash/", {"path": str(lock)})
    )
    assert moved.status_code == 200 and not lock.exists()
    assert api.cache.locks == {}


def test_stale_locks_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sync_daemon, "SYNC_DAEMON_CONFIG_PATH", tmp_path / "missing.toml"
    )
    response = sync_daemon_api.sync_daemon_stale_locks(
        _request("GET", "/api/sync/daemon/stale-locks/")
    )
    assert response.data["locks"] == []


def test_overview_reports_held_deletes_as_attention():
    status = _status(open_conflicts=0)
    status["roots"][0]["held_deletes"] = 3

    result = sync_state.overview(status)

    assert result["roots"][0]["held_deletes"] == 3
    assert result["roots"][1]["held_deletes"] == 0
    assert result["attention"]["held_deletes"] == [
        {"id": "projects", "path": "/Users/x/Projects", "count": 3}
    ]
    assert result["attention"]["needed"] is True


class HeldClient:
    released: list[tuple] = []
    folders: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def status(self):
        return {
            "roots": [
                {"id": "projects", "path": "/Users/x/Projects", "held_deletes": 2},
                {"id": "skills", "path": "/Users/x/.agents/skills", "held_deletes": 0},
            ]
        }

    def held_deletes(self, root, limit=None):
        assert limit == sync_state.HELD_DELETE_SAMPLE
        return ["a/gone.txt", "a"] if root == "projects" else []

    def held_folders(self, root, folder=None, limit=None):
        assert limit == sync_state.HELD_DELETE_SAMPLE
        return self.folders if root == "projects" else []

    def release_deletes(self, root, folder=None):
        HeldClient.released.append((root, folder))
        return 2

    def discard_deletes(self, root, folder=None):
        return 2


def test_held_deletes_api_lists_and_releases(monkeypatch):
    HeldClient.released = []
    monkeypatch.setattr(sync_daemon, "SyncDaemonClient", HeldClient)
    monkeypatch.setattr(sync_daemon, "is_configured", lambda config_path=None: True)
    assert (
        resolve_url("/api/sync/daemon/held-deletes/").url_name
        == "sync-daemon-held-deletes"
    )

    listed = sync_daemon_api.sync_daemon_held_deletes(
        _request("GET", "/api/sync/daemon/held-deletes/")
    )
    assert listed.status_code == 200
    assert listed.data["roots"] == [
        {
            "id": "projects",
            "path": "/Users/x/Projects",
            "count": 2,
            "sample": ["a/gone.txt", "a"],
            "folders": [],
        }
    ]

    bad = sync_daemon_api.sync_daemon_held_deletes(
        _request(
            "POST",
            "/api/sync/daemon/held-deletes/",
            {"root": "projects", "action": "delete"},
        )
    )
    assert bad.status_code == 400

    done = sync_daemon_api.sync_daemon_held_deletes(
        _request(
            "POST",
            "/api/sync/daemon/held-deletes/",
            {"root": "projects", "action": "release"},
        )
    )
    assert done.data == {"root": "projects", "action": "release", "count": 2}
    assert HeldClient.released == [("projects", None)]

    one = sync_daemon_api.sync_daemon_held_deletes(
        _request(
            "POST",
            "/api/sync/daemon/held-deletes/",
            {"root": "projects", "action": "release", "folder": "a/"},
        )
    )
    assert one.data == {
        "root": "projects",
        "action": "release",
        "count": 2,
        "folder": "a",
    }
    assert HeldClient.released[-1] == ("projects", "a")


def test_held_deletes_summary_lists_holds_by_folder():
    class FolderClient(HeldClient):
        def status(self):
            return {
                "roots": [
                    {
                        "id": "projects",
                        "path": "/Users/x/Projects",
                        "held_deletes": 2,
                        "held_folders": [{"folder": "a", "count": 2}],
                    }
                ]
            }

    FolderClient.folders = [
        {
            "folder": "a",
            "count": 2,
            "since": "2026-10-09T00:00:00Z",
            "rule": "count",
            "sample": ["a", "a/gone.txt"],
        }
    ]
    client = FolderClient()
    [root] = sync_state.held_deletes_summary(client, client.status())
    assert root["folders"] == [
        {
            "folder": "a",
            "count": 2,
            "since": "2026-10-09T00:00:00Z",
            "rule": "count",
            "sample": ["a", "a/gone.txt"],
        }
    ]

    status = _status(open_conflicts=0)
    status["roots"][0]["held_deletes"] = 2
    status["roots"][0]["held_folders"] = [{"folder": "a", "count": 2, "rule": "count"}]
    overview = sync_state.overview(status)
    assert overview["roots"][0]["held_folders"] == [
        {"folder": "a", "count": 2, "since": "", "rule": "count"}
    ]
    assert overview["roots"][1]["held_folders"] == []


def test_held_deletes_summary_keeps_engaged_hold_without_paths():
    class EmptyHoldClient:
        def held_deletes(self, root, limit=None):
            return []

        def held_folders(self, root, limit=None):
            return [{"folder": "burst", "count": 0, "rule": "count"}]

    payload = {
        "roots": [
            {
                "id": "projects",
                "held_deletes": 0,
                "held_folders": [{"folder": "burst", "count": 0}],
            }
        ]
    }
    [summary] = sync_state.held_deletes_summary(EmptyHoldClient(), payload)
    assert summary["count"] == 0
    assert summary["folders"][0]["folder"] == "burst"
