"""Root management for the Openbase Sync daemon config (``[[roots]]``)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from openbase_coder_cli import skills_sync, sync_daemon
from openbase_coder_cli.cli.sync_daemon import sync_daemon_cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sync_daemon, "OPENBASE_BASE_DIR", home / ".openbase")
    monkeypatch.setattr(skills_sync, "linked_source_paths", lambda: [])
    return home


def _configure(roots: list[dict], extra: str = "") -> Path:
    config = sync_daemon.SyncDaemonConfig(
        device_id="laptop",
        sync_group="default",
        role="edge",
        pair_secret="s",
        roots=roots,
        peer_hot="hub:22100",
        peer_bulk="hub:22101",
    )
    path = sync_daemon.write_config(config)
    if extra:
        path.write_text(path.read_text() + extra)
    return path


def test_roots_render_and_read_pins(home):
    _configure(
        [
            {"id": "projects", "path": "~/Projects", "pins": [".", "big/data"]},
            {"id": "skills", "path": "~/.agents/skills"},
        ]
    )

    data = tomllib.loads(sync_daemon.SYNC_DAEMON_CONFIG_PATH.read_text())
    assert data["roots"][0]["pins"] == [".", "big/data"]
    assert "pins" not in data["roots"][1]
    assert sync_daemon.configured_roots() == [
        {"id": "projects", "path": "~/Projects", "pins": [".", "big/data"]},
        {"id": "skills", "path": "~/.agents/skills"},
    ]
    summary = sync_daemon.read_config_summary()
    assert summary["role"] == "edge" and len(summary["roots"]) == 2


def test_set_roots_keeps_each_roots_ignores(home):
    _configure(
        [{"id": "projects", "path": "~/Projects"}],
        extra='ignore = ["/crm/data", "*.log"]\n',
    )
    assert sync_daemon.configured_roots()[0]["ignore"] == ["/crm/data", "*.log"]

    sync_daemon.add_roots(["~/Notes"])

    data = tomllib.loads(sync_daemon.SYNC_DAEMON_CONFIG_PATH.read_text())
    assert data["roots"][0]["ignore"] == ["/crm/data", "*.log"]
    assert "ignore" not in data["roots"][1]


def test_state_dir_follows_config(home, tmp_path):
    path = _configure([])
    assert sync_daemon.state_dir() == path.parent
    custom = tmp_path / "elsewhere"
    path.write_text(
        path.read_text().replace(
            f'state_dir = "{path.parent}"', f'state_dir = "{custom}"'
        )
    )
    assert sync_daemon.state_dir() == custom


def test_broken_config_summary_reports_error_instead_of_raising(home):
    path = sync_daemon.SYNC_DAEMON_CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text("role = \n")

    summary = sync_daemon.read_config_summary()

    assert summary["configured"] is True
    assert "unreadable config" in summary["error"]
    assert sync_daemon.configured_roots() == []


def test_set_roots_keeps_unmanaged_keys(home):
    _configure(
        [{"id": "projects", "path": "~/Projects"}],
        extra='\n[[roots]]\nid = "old"\npath = "~/old"\n\n[relay]\nexec_allow = ["open"]\n',
    )

    sync_daemon.set_roots([sync_daemon.root_entry("~/Notes")])

    data = tomllib.loads(sync_daemon.SYNC_DAEMON_CONFIG_PATH.read_text())
    assert [root["path"] for root in data["roots"]] == ["~/Notes"]
    assert data["relay"] == {"exec_allow": ["open"]}
    assert data["placement"]["anchor"] == "hub"
    assert data["role"] == "edge"


def test_set_roots_requires_a_config(home):
    with pytest.raises(sync_daemon.SyncDaemonError, match="configure"):
        sync_daemon.set_roots([])


def test_root_entry_is_home_relative(home, tmp_path):
    assert sync_daemon.root_entry(home / "Projects") == {
        "id": "projects",
        "path": "~/Projects",
    }
    assert sync_daemon.root_entry("~/.openbase/thread-sync")["id"] == (
        "openbase-thread-sync"
    )
    outside = tmp_path / "elsewhere"
    assert sync_daemon.root_entry(outside)["path"] == str(outside.resolve())


def test_plan_root_additions_never_nests_roots(home):
    existing = [sync_daemon.root_entry("~/Projects/app/data")]

    roots, change = sync_daemon.plan_root_additions(
        existing, ["~/Projects/app/data/sub", "~/Projects", "~/Notes"]
    )
    assert [root["path"] for root in roots] == ["~/Projects/app/data", "~/Notes"]
    assert [path for path, _ in change.skipped] == [
        "~/Projects/app/data/sub",
        "~/Projects",
    ]
    assert "--replace-nested" in change.skipped[1][1]

    roots, change = sync_daemon.plan_root_additions(
        existing, ["~/Projects"], replace_nested=True
    )
    assert [root["path"] for root in roots] == ["~/Projects"]
    assert [root["path"] for root in change.replaced] == ["~/Projects/app/data"]


def test_add_and_remove_roots(home):
    _configure([sync_daemon.root_entry("~/Projects")])

    change = sync_daemon.add_roots(["~/Projects", "~/.agents/skills"])
    assert [root["path"] for root in change.added] == ["~/.agents/skills"]
    assert sync_daemon.add_roots(["~/.agents/skills"]).changed is False

    removed = sync_daemon.remove_roots(["~/.agents/skills", "~/missing"])
    assert [root["path"] for root in removed] == ["~/.agents/skills"]
    assert [root["path"] for root in sync_daemon.configured_roots()] == ["~/Projects"]


def test_path_is_synced(home):
    assert sync_daemon.path_is_synced(home / ".openbase/thread-sync") is False
    _configure([sync_daemon.root_entry("~/.openbase/thread-sync")])

    assert sync_daemon.path_is_synced(home / ".openbase/thread-sync")
    assert sync_daemon.path_is_synced(home / ".openbase/thread-sync/devices/x")
    assert not sync_daemon.path_is_synced(home / ".openbase")
    assert not sync_daemon.path_is_synced(home / ".openbase/thread-sync-other")


def test_product_folder_roots_include_linked_skill_sources(home, monkeypatch):
    monkeypatch.setattr(
        skills_sync, "linked_source_paths", lambda: [home / "Developer/skills"]
    )

    assert sync_daemon.product_folder_roots() == [
        "~/.openbase/thread-sync",
        "~/.agents/skills",
        "~/Developer/skills",
    ]


def test_restart_only_when_service_installed(home, monkeypatch):
    calls = []
    monkeypatch.setattr(sync_daemon, "service_installed", lambda: False)
    monkeypatch.setattr(
        "openbase_coder_cli.services.launchd.install_service",
        lambda config, svc: calls.append(svc.name),
    )
    assert sync_daemon.restart_service_if_installed() is False
    assert calls == []


def test_configure_with_product_folders(home, monkeypatch):
    monkeypatch.setattr(sync_daemon, "default_device_id", lambda: "desktop-test")

    result = CliRunner().invoke(
        sync_daemon_cli,
        [
            "configure",
            "--role",
            "edge",
            "--peer",
            "mini",
            "--pair-secret",
            "s",
            "--root",
            "~/Projects",
            "--with-product-folders",
            "--no-start",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [root["path"] for root in sync_daemon.configured_roots()] == [
        "~/Projects",
        "~/.openbase/thread-sync",
        "~/.agents/skills",
    ]


def test_configure_product_folders_alone_and_requires_some_root(home, monkeypatch):
    monkeypatch.setattr(sync_daemon, "default_device_id", lambda: "desktop-test")
    runner = CliRunner()
    base = ["configure", "--role", "hub", "--listen", "100.64.0.1", "--no-start"]

    result = runner.invoke(sync_daemon_cli, base)
    assert result.exit_code != 0 and "--with-product-folders" in result.output

    result = runner.invoke(sync_daemon_cli, [*base, "--with-product-folders"])
    assert result.exit_code == 0, result.output
    assert len(sync_daemon.configured_roots()) == 2


def test_configure_skips_nested_roots(home, monkeypatch):
    monkeypatch.setattr(sync_daemon, "default_device_id", lambda: "desktop-test")

    result = CliRunner().invoke(
        sync_daemon_cli,
        [
            "configure",
            "--role",
            "hub",
            "--listen",
            "100.64.0.1",
            "--root",
            "~/Projects",
            "--root",
            "~/Projects/app",
            "--no-start",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Skipping root ~/Projects/app" in result.output
    assert [root["path"] for root in sync_daemon.configured_roots()] == ["~/Projects"]
