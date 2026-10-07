"""``openbase-coder sync migrate-from-syncthing``: temp HOME, fake launchd."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from openbase_coder_cli import skills_sync, sync_daemon, sync_migration
from openbase_coder_cli.cli.sync import sync
from openbase_coder_cli.services import launchd


class FakeLaunchd:
    """Records service calls; ``installed`` holds the loaded service names."""

    def __init__(self) -> None:
        self.installed: set[str] = set()
        self.removed: list[str] = []
        self.restarts = 0

    def status(self, svc):
        return {"installed": svc.name in self.installed}

    def remove(self, svc):
        existed = svc.name in self.installed
        self.installed.discard(svc.name)
        self.removed.append(svc.name)
        return existed


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    base = home / ".openbase"
    base.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sync_migration, "OPENBASE_BASE_DIR", base)
    monkeypatch.setattr(sync_daemon, "OPENBASE_BASE_DIR", base)
    monkeypatch.setattr(skills_sync, "linked_source_paths", lambda: [])
    return home


@pytest.fixture
def fake_launchd(home, monkeypatch):
    fake = FakeLaunchd()
    monkeypatch.setattr(launchd, "launchctl_status", fake.status)
    monkeypatch.setattr(launchd, "remove_service", fake.remove)
    monkeypatch.setattr(
        launchd, "_plist_path", lambda svc: home / "LaunchAgents" / f"{svc.name}.plist"
    )
    monkeypatch.setattr(
        launchd, "_wrapper_path", lambda svc: home / "wrappers" / f"{svc.name}.sh"
    )

    def restart():
        fake.restarts += 1
        return True

    monkeypatch.setattr(sync_daemon, "restart_service_if_installed", restart)
    return fake


def _legacy_install(home: Path, fake: FakeLaunchd) -> None:
    """The on-disk state an install of the previous sync leaves behind."""
    base = home / ".openbase"
    fake.installed.add("code-sync")
    (base / "code-sync").mkdir()
    (base / "code-sync" / "config.xml").write_text("<configuration/>")
    (base / "sync-versions" / "cs-1").mkdir(parents=True)
    (base / "sync-config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enabled": True,
                "folders": [
                    {
                        "relpath": "Projects",
                        "extra_ignores": ["// heading", "(?d)trash", "/big"],
                    },
                    {"relpath": "Projects/app", "extra_ignores": []},
                    {"relpath": ".agents/skills", "extra_ignores": []},
                    {"relpath": "Developer/skills", "extra_ignores": []},
                    {"relpath": ".openbase/codex_home/skills", "extra_ignores": []},
                    {"relpath": ".openbase/auth", "extra_ignores": []},
                ],
            }
        )
    )
    projects = home / "Projects"
    (projects / ".stfolder").mkdir(parents=True)
    (projects / ".stignore").write_text("(?d).git\n")
    (projects / "README.md").write_text("mine")


def _configure_daemon(roots: list[str]) -> None:
    sync_daemon.write_config(
        sync_daemon.SyncDaemonConfig(
            device_id="laptop",
            sync_group="default",
            role="edge",
            pair_secret="s",
            roots=[sync_daemon.root_entry(root) for root in roots],
            peer_hot="hub:22100",
            peer_bulk="hub:22101",
        )
    )


def _root_paths() -> list[str]:
    return [root["path"] for root in sync_daemon.configured_roots()]


def test_roots_for_legacy_config_filters_and_folds():
    config = sync_migration.LegacyConfig(
        enabled=True,
        folders=tuple(
            sync_migration.LegacyFolder(relpath)
            for relpath in (
                "Projects",
                "Projects/app",
                ".openbase/codex_home/skills",
                ".openbase/auth",
                ".agents/skills",
            )
        ),
    )

    assert sync_migration.roots_for_legacy_config(config) == [
        "~/Projects",
        "~/.agents/skills",
        "~/.openbase/thread-sync",
    ]
    disabled = sync_migration.LegacyConfig(enabled=False, folders=config.folders[:1])
    assert sync_migration.roots_for_legacy_config(disabled) == ["~/Projects"]


def test_unreadable_legacy_config_is_reported_not_raised(home):
    (home / ".openbase" / "sync-config.json").write_text("{not json")

    config = sync_migration.read_legacy_config()

    assert config.folders == () and "unreadable" in config.error


def test_never_had_syncthing_is_a_noop(home, fake_launchd):
    result = CliRunner().invoke(sync, ["migrate-from-syncthing", "--apply"])

    assert result.exit_code == 0, result.output
    assert "Nothing to migrate." in result.output
    assert fake_launchd.removed == []
    assert not (home / ".openbase" / "trash").exists()


def test_dry_run_changes_nothing(home, fake_launchd):
    _legacy_install(home, fake_launchd)
    _configure_daemon(["~/Notes"])
    before = sync_daemon.SYNC_DAEMON_CONFIG_PATH.read_text()

    result = CliRunner().invoke(sync, ["migrate-from-syncthing"])

    assert result.exit_code == 0, result.output
    assert "Would stop and uninstall the code-sync service." in result.output
    assert "Would add root ~/Projects" in result.output
    assert "2 custom ignore rule(s)" in result.output
    assert "Dry run: nothing was changed." in result.output
    assert fake_launchd.removed == [] and fake_launchd.restarts == 0
    assert (home / ".openbase" / "code-sync").is_dir()
    assert (home / "Projects" / ".stignore").is_file()
    assert sync_daemon.SYNC_DAEMON_CONFIG_PATH.read_text() == before


def test_apply_trashes_state_adds_roots_and_is_idempotent(home, fake_launchd):
    _legacy_install(home, fake_launchd)
    _configure_daemon(["~/Notes"])

    result = CliRunner().invoke(sync, ["migrate-from-syncthing", "--apply"])

    assert result.exit_code == 0, result.output
    assert fake_launchd.removed == ["code-sync"]
    base = home / ".openbase"
    for name in ("code-sync", "sync-versions", "sync-config.json"):
        assert not (base / name).exists()
    (trash,) = (base / "trash").iterdir()
    assert trash.name.startswith("syncthing-migration-")
    assert (trash / ".openbase" / "code-sync" / "config.xml").is_file()
    assert (trash / ".openbase" / "sync-versions" / "cs-1").is_dir()
    assert json.loads((trash / ".openbase" / "sync-config.json").read_text())
    # Markers stay unless explicitly requested.
    assert (home / "Projects" / ".stignore").is_file()
    assert (home / "Projects" / "README.md").read_text() == "mine"
    assert _root_paths() == [
        "~/Notes",
        "~/Projects",
        "~/.agents/skills",
        "~/Developer/skills",
        "~/.openbase/thread-sync",
    ]
    assert fake_launchd.restarts == 1
    assert "Migration complete." in result.output

    again = CliRunner().invoke(sync, ["migrate-from-syncthing", "--apply"])
    assert again.exit_code == 0, again.output
    assert "Nothing to migrate." in again.output
    assert fake_launchd.removed == ["code-sync"]
    assert fake_launchd.restarts == 1
    assert len(list((base / "trash").iterdir())) == 1


def test_remove_markers_moves_them_into_the_trash(home, fake_launchd):
    _legacy_install(home, fake_launchd)
    _configure_daemon(["~/Projects"])

    result = CliRunner().invoke(
        sync, ["migrate-from-syncthing", "--apply", "--remove-markers"]
    )

    assert result.exit_code == 0, result.output
    assert not (home / "Projects" / ".stignore").exists()
    assert not (home / "Projects" / ".stfolder").exists()
    (trash,) = (home / ".openbase" / "trash").iterdir()
    assert (trash / "Projects" / ".stignore").read_text() == "(?d).git\n"
    assert (home / "Projects" / "README.md").read_text() == "mine"


def test_remove_markers_after_state_was_already_migrated(home, fake_launchd):
    _legacy_install(home, fake_launchd)
    _configure_daemon(["~/Projects"])
    first = CliRunner().invoke(sync, ["migrate-from-syncthing", "--apply"])
    assert first.exit_code == 0, first.output
    (trash,) = (home / ".openbase" / "trash").iterdir()
    assert (home / "Projects" / ".stignore").is_file()

    second = CliRunner().invoke(
        sync, ["migrate-from-syncthing", "--apply", "--remove-markers"]
    )

    assert second.exit_code == 0, second.output
    assert not (home / "Projects" / ".stignore").exists()
    assert not (home / "Projects" / ".stfolder").exists()
    assert (trash / "Projects" / ".stignore").read_text() == "(?d).git\n"
    assert list((home / ".openbase" / "trash").iterdir()) == [trash]


def test_nested_existing_roots_are_kept_unless_replace_nested(home, fake_launchd):
    _legacy_install(home, fake_launchd)
    _configure_daemon(["~/Projects/app/data"])

    result = CliRunner().invoke(sync, ["migrate-from-syncthing", "--apply"])

    assert result.exit_code == 0, result.output
    assert "SKIP  ~/Projects" in result.output
    assert "~/Projects" not in _root_paths()
    assert "~/Projects/app/data" in _root_paths()


def test_replace_nested_swaps_inner_roots_for_the_folder(home, fake_launchd):
    _legacy_install(home, fake_launchd)
    _configure_daemon(["~/Projects/app/data"])

    result = CliRunner().invoke(
        sync, ["migrate-from-syncthing", "--apply", "--replace-nested"]
    )

    assert result.exit_code == 0, result.output
    assert "~/Projects/app/data" not in _root_paths()
    assert _root_paths()[0] == "~/Projects"


def test_unconfigured_daemon_gets_a_configure_hint(home, fake_launchd):
    _legacy_install(home, fake_launchd)

    result = CliRunner().invoke(sync, ["migrate-from-syncthing", "--apply"])

    assert result.exit_code == 0, result.output
    assert fake_launchd.removed == ["code-sync"]
    assert not sync_daemon.is_configured()
    assert "openbase-coder sync-daemon configure" in result.output
    assert "--root ~/Projects" in result.output
    assert fake_launchd.restarts == 0


def test_no_restart_flag(home, fake_launchd):
    _legacy_install(home, fake_launchd)
    _configure_daemon(["~/Notes"])

    result = CliRunner().invoke(
        sync, ["migrate-from-syncthing", "--apply", "--no-restart"]
    )

    assert result.exit_code == 0, result.output
    assert fake_launchd.restarts == 0
    assert "~/Projects" in _root_paths()


def test_leftover_service_files_count_as_installed(home, fake_launchd):
    wrapper = home / "wrappers" / "code-sync.sh"
    wrapper.parent.mkdir()
    wrapper.write_text("#!/bin/sh\n")

    plan = sync_migration.plan_migration()

    assert plan.service_installed and not plan.nothing_to_do


def test_trash_folder_name_is_unique(home):
    now = datetime(2026, 10, 7, 12, 0, 0)
    first = sync_migration._unique_trash_folder(now)
    first.mkdir(parents=True)

    second = sync_migration._unique_trash_folder(now)

    assert first.name == "syncthing-migration-20261007-120000"
    assert second.name == "syncthing-migration-20261007-120000-2"
