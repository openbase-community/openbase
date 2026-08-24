from __future__ import annotations

import socket
from pathlib import Path

import pytest

from openbase_coder_cli.code_sync import CodeSyncError, manager
from openbase_coder_cli.sync_config import SyncFolder


def test_port_guard_raises_when_port_taken(monkeypatch) -> None:
    monkeypatch.setattr(manager, "_managed_service_running", lambda: False)
    monkeypatch.setattr(manager, "_user_managed_syncthing_running", lambda: True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        port = sock.getsockname()[1]
        monkeypatch.setattr(manager, "_listen_port_available", lambda p=port: False)
        with pytest.raises(CodeSyncError, match="user-managed"):
            manager.ensure_port_available()


def test_port_guard_skipped_when_own_service_running(monkeypatch) -> None:
    monkeypatch.setattr(manager, "_managed_service_running", lambda: True)
    monkeypatch.setattr(manager, "_listen_port_available", lambda: False)
    manager.ensure_port_available()  # must not raise


def test_user_syncthing_folder_parsing(tmp_path: Path) -> None:
    config_dir = tmp_path / "Library" / "Application Support" / "Syncthing"
    config_dir.mkdir(parents=True)
    (config_dir / "config.xml").write_text(
        """<configuration version="37">
  <folder id="projects" path="{home}/Projects" type="sendreceive"></folder>
  <folder id="desktop" path="{home}/Desktop" type="sendreceive"></folder>
</configuration>
""".format(home=tmp_path),
        encoding="utf-8",
    )

    folders = manager.user_managed_syncthing_folders(home=tmp_path)

    assert tmp_path / "Projects" in folders
    assert tmp_path / "Desktop" in folders


def test_overlap_guard_rejects_shared_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(manager, "_user_managed_syncthing_running", lambda: True)
    monkeypatch.setattr(
        manager,
        "user_managed_syncthing_folders",
        lambda home=None: [tmp_path / "Projects"],
    )

    nested = [SyncFolder(relpath="Projects/demo")]
    with pytest.raises(CodeSyncError, match="overlaps"):
        manager.ensure_no_user_managed_overlap(nested, home=tmp_path)

    disjoint = [SyncFolder(relpath="Documents/notes")]
    manager.ensure_no_user_managed_overlap(disjoint, home=tmp_path)  # ok


def test_overlap_guard_noop_when_no_user_syncthing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(manager, "_user_managed_syncthing_running", lambda: False)
    manager.ensure_no_user_managed_overlap(
        [SyncFolder(relpath="Projects/demo")], home=tmp_path
    )


def test_syncthing_process_matcher_ignores_scripts(monkeypatch) -> None:
    class FakeResult:
        returncode = 0
        stdout = (
            "100 /Users/u/.openbase/bin/syncthing serve --config "
            "/Users/u/.openbase/code-sync\n"
            "200 bash -c curl http://127.0.0.1:8384/rest/db/status syncthing\n"
        )

    monkeypatch.setattr(manager.subprocess, "run", lambda *a, **k: FakeResult())
    monkeypatch.setattr(manager, "CODE_SYNC_DIR", Path("/Users/u/.openbase/code-sync"))
    assert manager._user_managed_syncthing_running() is False

    FakeResult.stdout = (
        "300 /Applications/Syncthing.app/Contents/Resources/syncthing/syncthing "
        "--no-browser\n"
    )
    assert manager._user_managed_syncthing_running() is True


def test_accept_pending_folders_adopts_valid_offers(monkeypatch, tmp_path) -> None:
    from openbase_coder_cli import sync_config

    config_path = tmp_path / "sync-config.json"
    sync_config.set_sync_folders([{"relpath": "Projects"}], config_path)
    good_id = sync_config.folder_id_for_relpath("Developer")

    pending = {
        good_id: {"offeredBy": {"PEER": {"label": "Developer"}}},
        "cs-wrongid": {"offeredBy": {"PEER": {"label": "Business"}}},
        sync_config.folder_id_for_relpath("../evil"): {
            "offeredBy": {"PEER": {"label": "../evil"}}
        },
        sync_config.folder_id_for_relpath("Projects"): {
            "offeredBy": {"PEER": {"label": "Projects"}}
        },
    }

    class FakeClient:
        def pending_folders(self):
            return pending

    monkeypatch.setattr(manager, "SyncthingClient", FakeClient)
    applied = []
    monkeypatch.setattr(
        manager, "apply_settings_change", lambda cp=None: applied.append(cp)
    )

    accepted = manager.accept_pending_folders(config_path)

    assert accepted == ["Developer"]
    from openbase_coder_cli.sync_config import sync_folders

    relpaths = [f.relpath for f in sync_folders(config_path)]
    assert "Developer" in relpaths
    assert "Business" not in relpaths
    assert applied == [config_path]


def test_accept_pending_folders_noop_when_engine_down(monkeypatch, tmp_path) -> None:
    class DownClient:
        def __init__(self):
            raise CodeSyncError("no api key")

    monkeypatch.setattr(manager, "SyncthingClient", DownClient)
    assert manager.accept_pending_folders(tmp_path / "sync-config.json") == []


def test_ensure_product_state_folders_adds_missing(monkeypatch, tmp_path) -> None:
    from pathlib import Path as P

    from openbase_coder_cli import sync_config

    monkeypatch.setattr(P, "home", classmethod(lambda cls: tmp_path))
    config_path = tmp_path / "sync-config.json"
    sync_config.set_sync_folders([{"relpath": "Projects"}], config_path)

    added = manager.ensure_product_state_folders(config_path)

    # The shared agent homes (~/.codex, ~/.claude) never sync; thread-sync is
    # the only first-class product-state folder.
    assert set(sync_config.PRODUCT_STATE_RELPATHS) == {".openbase/thread-sync"}
    assert set(added) == set(sync_config.PRODUCT_STATE_RELPATHS)
    relpaths = {f.relpath for f in sync_config.sync_folders(config_path)}
    assert set(sync_config.PRODUCT_STATE_RELPATHS) <= relpaths
    assert (tmp_path / ".openbase/thread-sync").is_dir()

    # Idempotent.
    assert manager.ensure_product_state_folders(config_path) == []


def test_enable_installs_only_the_engine_service(monkeypatch) -> None:
    # Device sync no longer rides along as companion services: its workers
    # live in the always-installed sync-workers service and gate on
    # code_sync_enabled() at runtime.
    installed = []
    monkeypatch.setattr(
        "openbase_coder_cli.services.launchd.install_service",
        lambda installation, svc: installed.append(svc.name),
    )
    monkeypatch.setattr(
        "openbase_coder_cli.services.registry.require_installation",
        lambda: object(),
    )
    manager.install_and_start_service()
    assert installed == ["code-sync"]
