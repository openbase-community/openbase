"""``/api/sync/status|conflicts|conflicts/resolve`` keep the phone-app shapes."""

from __future__ import annotations

# ruff: noqa: E402, I001

import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django

django.setup()

import pytest
from django.urls import resolve as resolve_url
from rest_framework.test import APIRequestFactory, force_authenticate

from openbase_coder_cli import sync_daemon
from openbase_coder_cli.openbase_coder_cli_app import sync_daemon_api


def _request(method: str, path: str, data: dict | None = None):
    factory = APIRequestFactory()
    fn = {"GET": factory.get, "POST": factory.post}[method]
    request = fn(path, data=data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


class FakeClient:
    fail = False
    resolved: list[tuple[int, str]] = []

    def __init__(self, *args, **kwargs):
        pass

    def status(self):
        if FakeClient.fail:
            raise sync_daemon.SyncDaemonError("unreachable")
        return {
            "role": "edge",
            "roots": [
                {
                    "id": "projects",
                    "path": "~/Projects",
                    "seq": 10,
                    "pending_fetches": 0,
                    "scanning": False,
                }
            ],
            "peers": [
                {"device": "mini", "roots": {"projects": {"acked_seq": 5}}},
            ],
            "open_conflicts": 2,
        }

    def conflicts(self, root=None):
        if FakeClient.fail:
            raise sync_daemon.SyncDaemonError("unreachable")
        return [
            {
                "id": 7,
                "root": "projects",
                "path": "app/main.py",
                "kind": "content",
                "b_device": "mini",
                "created_ns": 1_760_000_000_000_000_000,
            },
            {
                "id": 8,
                "root": "projects",
                "path": "app:refs/heads/develop",
                "kind": "git-branch",
                "a_hash": "a" * 40,
                "b_hash": "b" * 40,
                "label": "",
            },
        ]

    def resolve(self, conflict_id, choice):
        FakeClient.resolved.append((conflict_id, choice))


@pytest.fixture
def configured(monkeypatch):
    FakeClient.fail = False
    FakeClient.resolved = []
    monkeypatch.setattr(sync_daemon, "SyncDaemonClient", FakeClient)
    monkeypatch.setattr(sync_daemon, "is_configured", lambda config_path=None: True)
    monkeypatch.setattr(
        sync_daemon,
        "configured_roots",
        lambda config_path=None: [{"id": "projects", "path": "~/Projects"}],
    )


def test_routes_point_at_daemon_views():
    assert resolve_url("/api/sync/status/").url_name == "sync-status"
    assert resolve_url("/api/sync/conflicts/").url_name == "sync-conflicts"
    assert (
        resolve_url("/api/sync/conflicts/resolve/").url_name == "sync-conflicts-resolve"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/sync/settings/",
        "/api/sync/peers/remove/",
        "/api/sync/versions/purge/",
        "/api/sync/conflicts/ignore-containing-folder/",
        "/api/sync/git/cs-1/repo/info/refs",
    ],
)
def test_syncthing_routes_are_gone(path):
    # Only the console's catch-all page route matches what is left.
    assert resolve_url(path).url_name is None


def test_status_unconfigured(monkeypatch):
    monkeypatch.setattr(sync_daemon, "is_configured", lambda config_path=None: False)

    response = sync_daemon_api.sync_status(_request("GET", "/api/sync/status/"))

    assert response.status_code == 200
    assert response.data["enabled"] is False
    assert response.data["folders"] == []


def test_status_maps_roots_to_folders(configured):
    response = sync_daemon_api.sync_status(_request("GET", "/api/sync/status/"))

    assert response.status_code == 200
    assert response.data["enabled"] is True
    assert response.data["conflicts_count"] == 2
    (folder,) = response.data["folders"]
    assert folder["id"] == "projects"
    assert folder["relpath"] == "Projects"
    assert folder["state"] == "idle"
    assert folder["peer_completion"] == {"mini": 50.0}
    assert folder["completion"] == 50.0
    assert folder["receive_only"] is False


def test_status_daemon_down_still_lists_roots(configured):
    FakeClient.fail = True

    response = sync_daemon_api.sync_status(_request("GET", "/api/sync/status/"))

    assert response.status_code == 200
    assert response.data["folders"][0]["state"] == "unreachable"


def test_conflicts_use_legacy_shape_with_string_ids(configured):
    response = sync_daemon_api.sync_conflicts(_request("GET", "/api/sync/conflicts/"))

    assert response.status_code == 200
    assert response.data["unresolved_count"] == 2
    file_conflict, branch_conflict = response.data["conflicts"]
    assert file_conflict["id"] == "7"
    assert file_conflict["type"] == "file-conflict"
    assert file_conflict["files"] == ["app/main.py"]
    assert file_conflict["folder_relpath"] == "Projects"
    assert file_conflict["conflict_device_hint"] == "mini"
    assert file_conflict["detected_at"].startswith("2025-10-09T")
    assert branch_conflict["type"] == "repo-divergence"
    assert branch_conflict["repo_relpath"] == "app"
    assert branch_conflict["branch"] == "develop"
    assert branch_conflict["local_sha"] == "a" * 40
    assert branch_conflict["remote_sha"] == "b" * 40


def test_conflicts_unconfigured_is_empty(monkeypatch):
    monkeypatch.setattr(sync_daemon, "is_configured", lambda config_path=None: False)

    response = sync_daemon_api.sync_conflicts(_request("GET", "/api/sync/conflicts/"))

    assert response.data == {"conflicts": [], "unresolved_count": 0}


def test_resolve_accepts_string_ids_and_rejects_bad_ones(configured):
    ok = sync_daemon_api.sync_daemon_conflicts_resolve(
        _request(
            "POST", "/api/sync/conflicts/resolve/", {"id": "7", "action": "use_remote"}
        )
    )
    assert ok.status_code == 200
    assert FakeClient.resolved == [(7, "b")]

    bad = sync_daemon_api.sync_daemon_conflicts_resolve(
        _request(
            "POST",
            "/api/sync/conflicts/resolve/",
            {"id": "abc", "action": "keep_local"},
        )
    )
    assert bad.status_code == 400


def test_resolve_refuses_branch_divergence(configured):
    response = sync_daemon_api.sync_daemon_conflicts_resolve(
        _request(
            "POST", "/api/sync/conflicts/resolve/", {"id": "8", "action": "keep_local"}
        )
    )

    assert response.status_code == 409
    assert "neither" in response.data["error"]
    assert FakeClient.resolved == []
