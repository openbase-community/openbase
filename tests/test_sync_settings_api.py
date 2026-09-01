from __future__ import annotations

# ruff: noqa: E402, I001

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django
from rest_framework.test import APIRequestFactory, force_authenticate

django.setup()

from openbase_coder_cli import sync_config
from openbase_coder_cli.code_sync import conflicts as conflicts_module
from openbase_coder_cli.code_sync import manager as sync_manager
from openbase_coder_cli.code_sync.eligibility import EligibilityResult, SyncPeer
from openbase_coder_cli.openbase_coder_cli_app import sync_settings


def _authenticated_request(method: str, path: str, data: dict | None = None):
    factory = APIRequestFactory()
    request_factory = {
        "GET": factory.get,
        "PUT": factory.put,
        "POST": factory.post,
    }[method]
    request = request_factory(path, data=data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def _fake_eligibility(eligible: bool = True) -> EligibilityResult:
    return EligibilityResult(
        eligible=eligible,
        reason="" if eligible else "Add a second machine to enable sync.",
        peers=(
            SyncPeer(
                device_id="desktop-peer",
                name="Mac mini",
                kind="desktop",
                tailscale_magic_dns="mini.tail1234.ts.net.",
                syncthing_device_id="PEER-DEVICE-ID",
            ),
        ),
    )


def _patch_environment(monkeypatch, tmp_path: Path) -> Path:
    config_path = tmp_path / "sync-config.json"
    monkeypatch.setattr(sync_config, "SYNC_CONFIG_PATH", config_path)
    monkeypatch.setattr(
        sync_settings, "current_eligibility", lambda: _fake_eligibility()
    )
    monkeypatch.setattr(sync_settings, "stored_device_id", lambda: "SELF-DEVICE-ID")
    monkeypatch.setattr(sync_manager, "versions_usage_bytes", lambda: 1234)
    return config_path


def test_sync_settings_get_payload(monkeypatch, tmp_path: Path) -> None:
    _patch_environment(monkeypatch, tmp_path)
    sync_config.set_sync_folders(
        [{"relpath": "Projects/demo", "extra_ignores": ["*.log"]}]
    )
    sync_config.set_code_sync_enabled(True)

    response = sync_settings.sync_settings(
        _authenticated_request("GET", "/api/sync/settings/")
    )

    assert response.status_code == 200
    assert response.data["schema_version"] == 1
    assert response.data["enabled"] is True
    assert response.data["eligible"] is True
    assert response.data["self_device_id"] == "SELF-DEVICE-ID"
    assert response.data["peers"][0]["syncthing_device_id"] == "PEER-DEVICE-ID"
    assert response.data["folders"] == [
        {
            "id": sync_config.folder_id_for_relpath("Projects/demo"),
            "relpath": "Projects/demo",
            "extra_ignores": ["*.log"],
        }
    ]
    assert response.data["versions_usage_bytes"] == 1234
    assert response.data["lease_mode"] == "auto"


def test_sync_settings_put_replaces_folders_and_applies(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_environment(monkeypatch, tmp_path)
    sync_config.set_code_sync_enabled(True)
    sync_config.set_sync_folders([{"relpath": "Projects/old"}])
    applied: list[str] = []
    monkeypatch.setattr(
        sync_manager,
        "apply_settings_change",
        lambda config_path=None: applied.append("apply") or {"applied": True},
    )

    response = sync_settings.sync_settings(
        _authenticated_request(
            "PUT",
            "/api/sync/settings/",
            {
                "folders": [{"relpath": "Projects/new", "extra_ignores": ["*.bin"]}],
                "lease_mode": "manual",
            },
        )
    )

    assert response.status_code == 200
    assert [folder["relpath"] for folder in response.data["folders"]] == [
        "Projects/new"
    ]
    assert response.data["lease_mode"] == "manual"
    assert applied == ["apply"]


def test_sync_settings_put_enables_and_disables(monkeypatch, tmp_path: Path) -> None:
    _patch_environment(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        sync_manager,
        "enable_code_sync",
        lambda force=False, config_path=None: (
            calls.append("enable"),
            sync_config.set_code_sync_enabled(True),
        ),
    )
    monkeypatch.setattr(
        sync_manager,
        "disable_code_sync",
        lambda config_path=None: (
            calls.append("disable"),
            sync_config.set_code_sync_enabled(False),
        ),
    )

    response = sync_settings.sync_settings(
        _authenticated_request("PUT", "/api/sync/settings/", {"enabled": True})
    )
    assert response.status_code == 200
    assert calls == ["enable"]
    assert sync_config.code_sync_enabled() is True

    response = sync_settings.sync_settings(
        _authenticated_request("PUT", "/api/sync/settings/", {"enabled": False})
    )
    assert response.status_code == 200
    assert calls == ["enable", "disable"]


def test_sync_settings_put_rejects_bad_relpath(monkeypatch, tmp_path: Path) -> None:
    _patch_environment(monkeypatch, tmp_path)

    response = sync_settings.sync_settings(
        _authenticated_request(
            "PUT",
            "/api/sync/settings/",
            {"folders": [{"relpath": "../etc"}]},
        )
    )

    assert response.status_code == 400
    assert sync_config.sync_folders() == ()


def test_sync_settings_put_requires_a_field(monkeypatch, tmp_path: Path) -> None:
    _patch_environment(monkeypatch, tmp_path)
    response = sync_settings.sync_settings(
        _authenticated_request("PUT", "/api/sync/settings/", {})
    )
    assert response.status_code == 400


def test_sync_conflicts_list_and_resolve(monkeypatch, tmp_path: Path) -> None:
    conflicts_path = tmp_path / "conflicts.json"
    monkeypatch.setattr(conflicts_module, "CODE_SYNC_CONFLICTS_PATH", conflicts_path)
    record = conflicts_module.record_branch_conflict(
        folder_id="cs-test",
        repo_relpath="repo",
        branch="main",
        local_sha="a" * 40,
        remote_sha="b" * 40,
    )

    response = sync_settings.sync_conflicts(
        _authenticated_request("GET", "/api/sync/conflicts/")
    )
    assert response.status_code == 200
    assert response.data["unresolved_count"] == 1
    assert response.data["conflicts"][0]["id"] == record["id"]
    assert response.data["conflicts"][0]["type"] == "repo-divergence"
    assert response.data["conflicts"][0]["kind"] == "branch"
    assert response.data["conflicts"][0]["repo_relpath"] == "repo"
    assert response.data["conflicts"][0]["folder_relpath"] == ""

    response = sync_settings.sync_conflicts_resolve(
        _authenticated_request(
            "POST",
            "/api/sync/conflicts/resolve/",
            {"id": record["id"], "action": "keep_local"},
        )
    )
    assert response.status_code == 200
    assert response.data["conflict"]["resolved"] is True

    response = sync_settings.sync_conflicts(
        _authenticated_request("GET", "/api/sync/conflicts/")
    )
    assert response.status_code == 200
    assert response.data["unresolved_count"] == 0
    assert response.data["conflicts"] == []

    response = sync_settings.sync_conflicts_resolve(
        _authenticated_request(
            "POST",
            "/api/sync/conflicts/resolve/",
            {"id": "missing", "action": "keep_local"},
        )
    )
    assert response.status_code == 400


def test_sync_conflict_ignore_containing_folder(monkeypatch, tmp_path: Path) -> None:
    _patch_environment(monkeypatch, tmp_path)
    conflicts_path = tmp_path / "conflicts.json"
    monkeypatch.setattr(conflicts_module, "CODE_SYNC_CONFLICTS_PATH", conflicts_path)
    apply_calls = []
    monkeypatch.setattr(
        sync_manager, "apply_settings_change", lambda: apply_calls.append(True)
    )
    sync_config.set_sync_folders(
        [{"relpath": "Projects", "extra_ignores": ["/already-ignored"]}]
    )
    folder_id = sync_config.folder_id_for_relpath("Projects")
    target = conflicts_module.record_file_conflict(
        folder_id=folder_id,
        file_relpath="openbase/data/db/file.sync-conflict-20260713-112233-ABC1234.txt",
    )
    sibling = conflicts_module.record_file_conflict(
        folder_id=folder_id,
        file_relpath=(
            "openbase/data/db/other.sync-conflict-20260713-112234-ABC1234.txt"
        ),
    )
    outside = conflicts_module.record_file_conflict(
        folder_id=folder_id,
        file_relpath="openbase/data/logs/app.sync-conflict-20260713-112235-ABC1234.txt",
    )

    response = sync_settings.sync_conflicts(
        _authenticated_request("GET", "/api/sync/conflicts/")
    )
    assert response.status_code == 200
    conflict = next(
        item for item in response.data["conflicts"] if item["id"] == target["id"]
    )
    assert conflict["type"] == "file-conflict"
    assert conflict["kind"] == "file"
    assert conflict["folder_relpath"] == "Projects"
    assert conflict["path"] == (
        "openbase/data/db/file.sync-conflict-20260713-112233-ABC1234.txt"
    )
    assert conflict["files"] == [
        "openbase/data/db/file.sync-conflict-20260713-112233-ABC1234.txt"
    ]
    assert conflict["containing_folder"] == "openbase/data/db"
    assert conflict["original_path"] == "openbase/data/db/file.txt"
    assert conflict["conflict_device_hint"] == "ABC1234"
    assert conflict["ignored_containing_folder"] is False

    response = sync_settings.sync_conflicts_ignore_containing_folder(
        _authenticated_request(
            "POST",
            "/api/sync/conflicts/ignore-containing-folder/",
            {"id": target["id"]},
        )
    )

    assert response.status_code == 200
    assert response.data["ignore_pattern"] == "/openbase/data/db"
    assert response.data["added"] is True
    assert response.data["resolved_count"] == 2
    assert apply_calls == [True]
    assert sync_config.sync_folders()[0].extra_ignores == (
        "/already-ignored",
        "/openbase/data/db",
    )
    conflicts = {item["id"]: item for item in conflicts_module.read_conflicts()}
    assert conflicts[target["id"]]["resolved"] is True
    assert conflicts[sibling["id"]]["resolved"] is True
    assert conflicts[outside["id"]].get("resolved") is not True


def test_sync_versions_purge_reports_freed_bytes(monkeypatch, tmp_path: Path) -> None:
    versions_dir = tmp_path / "sync-versions"
    (versions_dir / "cs-demo").mkdir(parents=True)
    (versions_dir / "cs-demo" / "old.py").write_text("x" * 100, encoding="utf-8")
    monkeypatch.setattr(sync_manager, "SYNC_VERSIONS_DIR", versions_dir)

    response = sync_settings.sync_versions_purge(
        _authenticated_request("POST", "/api/sync/versions/purge/")
    )

    assert response.status_code == 200
    assert response.data["freed_bytes"] == 100
    assert not (versions_dir / "cs-demo").exists()


def test_sync_status_surfaces_folder_errors_and_reconcile_summary(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = _patch_environment(monkeypatch, tmp_path)
    sync_config.set_code_sync_enabled(True, config_path)
    sync_config.set_sync_folders([{"relpath": "Projects/demo"}], config_path)
    monkeypatch.setattr(
        conflicts_module, "CODE_SYNC_CONFLICTS_PATH", tmp_path / "conflicts.json"
    )
    monkeypatch.setattr(sync_settings, "local_activity_recent", lambda: False)
    monkeypatch.setattr(
        sync_settings,
        "read_reconcile_state",
        lambda: {
            "last_reconcile_at": "2026-08-17T22:00:00Z",
            "last_summary": {"repo_count": 3, "fast_forwarded": 1, "errors": 0},
        },
    )

    class FakeClient:
        def folder_status(self, folder_id: str) -> dict:
            return {
                "state": "stopped",
                "error": "insufficient space on disk for database",
            }

        def folder_completion(self, folder_id: str) -> dict:
            return {"completion": 42}

        def folder_config(self, folder_id: str) -> dict:
            return {"type": "sendreceive"}

        def system_errors(self) -> list[dict]:
            return [
                {
                    "when": "2026-08-17T21:59:00Z",
                    "message": "folder stopped: out of disk",
                }
            ]

    monkeypatch.setattr(sync_settings, "SyncthingClient", FakeClient)

    response = sync_settings.sync_status(
        _authenticated_request("GET", "/api/sync/status/")
    )

    assert response.status_code == 200
    folder = response.data["folders"][0]
    assert folder["state"] == "stopped"
    assert folder["error"] == "insufficient space on disk for database"
    assert response.data["syncthing_errors"] == [
        {"when": "2026-08-17T21:59:00Z", "message": "folder stopped: out of disk"}
    ]
    assert response.data["last_reconcile"] == {
        "at": "2026-08-17T22:00:00Z",
        "repo_count": 3,
        "fast_forwarded": 1,
        "errors": 0,
    }
