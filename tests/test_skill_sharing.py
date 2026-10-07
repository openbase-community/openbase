from __future__ import annotations

import json
from pathlib import Path

import pytest

from openbase_coder_cli import (
    dispatcher_config,
    skills_autolink,
    skills_sync,
    sync_daemon,
)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        skills_sync, "STATE_PATH", tmp_path / ".openbase/skill-sync.json"
    )
    monkeypatch.setattr(
        dispatcher_config,
        "CODEX_DISPATCHER_CONFIG_PATH",
        tmp_path / ".openbase/dispatcher-config.json",
    )
    monkeypatch.setattr(skills_autolink, "CODEX_HOME_DIR", tmp_path / ".codex")
    monkeypatch.setattr(skills_autolink, "CLAUDE_CONFIG_DIR", tmp_path / ".claude")
    return tmp_path


@pytest.fixture
def daemon(homes, monkeypatch):
    """Openbase Sync configured with ~/Projects; restarts are recorded."""
    restarts: list[bool] = []
    monkeypatch.setattr(
        sync_daemon,
        "restart_service_if_installed",
        lambda: restarts.append(True) or True,
    )
    sync_daemon.write_config(
        sync_daemon.SyncDaemonConfig(
            device_id="laptop",
            sync_group="default",
            role="edge",
            pair_secret="s",
            roots=[sync_daemon.root_entry("~/Projects")],
            peer_hot="hub:22100",
            peer_bulk="hub:22101",
        )
    )
    return restarts


def root_paths() -> list[str]:
    return [root["path"] for root in sync_daemon.configured_roots()]


def skill(root, name, content="original"):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(content)
    return folder


def test_backend_links_include_codex_only_and_never_replace_conflicts(homes):
    source = skill(homes / ".codex/skills", "codex-only")
    conflict = skill(homes / ".claude/skills", "codex-only", "keep me")
    dispatcher_config.set_auto_link_personal_skills(True)
    result = skills_autolink.sync_auto_linked_skills()
    assert result["conflicts"] > 0
    assert (homes / ".agents/skills/codex-only").resolve() == source
    assert not conflict.is_symlink()
    assert (conflict / "SKILL.md").read_text() == "keep me"
    assert (source / "SKILL.md").read_text() == "original"


def test_disabling_linking_keeps_existing_links_and_stops_new_links(homes):
    original = skill(homes / ".agents/skills", "existing")
    dispatcher_config.set_auto_link_personal_skills(True)
    skills_autolink.sync_auto_linked_skills()
    dispatcher_config.set_auto_link_personal_skills(False)
    skill(homes / ".agents/skills", "new-skill")
    assert skills_autolink.sync_auto_linked_skills()["created"] == 0
    for backend in [".codex", ".claude"]:
        assert (homes / backend / "skills/existing").resolve() == original
        assert not (homes / backend / "skills/new-skill").exists()


def test_sync_off_preserves_files_and_unrelated_roots(homes, daemon):
    original = skill(homes / ".agents/skills", "original")
    source = skill(homes / "skill-sources", "linked")
    (homes / ".agents/skills/linked").symlink_to(source)
    result = skills_sync.set_enabled(True)
    assert root_paths() == [
        "~/Projects",
        "~/.agents/skills",
        "~/skill-sources/linked",
    ]
    assert result["restarted"] is True
    skills_sync.set_enabled(False)
    assert root_paths() == ["~/Projects"]
    assert (original / "SKILL.md").read_text() == "original"
    assert (source / "SKILL.md").read_text() == "original"
    assert (homes / ".agents/skills/linked").is_symlink()
    assert daemon == [True, True]
    skills_sync.reconcile()
    assert root_paths() == ["~/Projects"]
    skills_sync.set_enabled(True)
    assert len(root_paths()) == 3


def test_existing_skills_root_is_recognized_without_claiming_unrelated_roots(
    homes, daemon
):
    source = skill(homes / "Projects/skill-source", "sample")
    (homes / ".agents/skills").mkdir(parents=True)
    (homes / ".agents/skills/sample").symlink_to(source)
    sync_daemon.add_roots(["~/.agents/skills"])
    assert skills_sync.enabled()
    # The linked source is already inside the ~/Projects root.
    assert not skills_sync.reconcile()["added"]
    skills_sync.set_enabled(False)
    assert root_paths() == ["~/Projects"]


def test_new_linked_sources_are_registered_on_later_ticks(homes, daemon):
    skills_sync.set_enabled(True)
    new = skill(homes / "sources", "new")
    (homes / ".agents/skills/new").symlink_to(new)
    assert skills_sync.reconcile()["added"] == ["~/sources/new"]
    assert skills_sync.reconcile()["added"] == []


@pytest.mark.parametrize("source_root", [".", ".ssh", ".codex/plugins/cache"])
def test_linked_sources_never_share_home_credentials_or_plugin_caches(
    homes, daemon, source_root
):
    source = homes / source_root
    source.mkdir(parents=True, exist_ok=True)
    (source / "SKILL.md").write_text("example")
    personal = homes / ".agents/skills"
    personal.mkdir(parents=True)
    (personal / "unsafe-source").symlink_to(source)
    result = skills_sync.set_enabled(True)
    assert root_paths() == ["~/Projects", "~/.agents/skills"]
    assert result["warnings"]


def test_failed_enable_restores_preferences(homes, daemon, monkeypatch):
    def fail(*args, **kwargs):
        raise sync_daemon.SyncDaemonError("config unwritable")

    monkeypatch.setattr(sync_daemon, "add_roots", fail)
    with pytest.raises(sync_daemon.SyncDaemonError):
        skills_sync.set_enabled(True)
    assert not skills_sync.enabled()
    assert root_paths() == ["~/Projects"]


def test_enable_without_openbase_sync_keeps_the_preference(homes):
    result = skills_sync.set_enabled(True)
    assert skills_sync.enabled()
    assert result["added"] == []
    assert "not set up" in result["warnings"][0]
    payload = skills_sync.settings_payload()
    assert payload["sync_skills_across_devices"] is True
    assert payload["device_sync_enabled"] is False


def test_disable_removes_roots_recorded_by_older_releases(homes, daemon):
    sync_daemon.add_roots(["~/.agents/skills", "~/Developer/skills"])
    skills_sync.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    skills_sync.STATE_PATH.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enabled": True,
                "managed_folders": ["Developer/skills"],
            }
        )
    )
    skills_sync.set_enabled(False)
    assert root_paths() == ["~/Projects"]


def test_refuses_future_state_schema(homes):
    skills_sync.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    skills_sync.STATE_PATH.write_text(json.dumps({"schema_version": 999}))
    with pytest.raises(ValueError, match="update Openbase"):
        skills_sync.enabled()
