from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openbase_coder_cli import (
    dispatcher_config,
    skills_autolink,
    skills_sync,
    sync_config,
)
from openbase_coder_cli.code_sync import CodeSyncError, manager


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        skills_sync, "STATE_PATH", tmp_path / ".openbase/skill-sync.json"
    )
    monkeypatch.setattr(
        sync_config, "SYNC_CONFIG_PATH", tmp_path / ".openbase/sync-config.json"
    )
    monkeypatch.setattr(
        dispatcher_config,
        "CODEX_DISPATCHER_CONFIG_PATH",
        tmp_path / ".openbase/dispatcher-config.json",
    )
    monkeypatch.setattr(skills_autolink, "CODEX_HOME_DIR", tmp_path / ".codex")
    monkeypatch.setattr(skills_autolink, "CLAUDE_CONFIG_DIR", tmp_path / ".claude")
    monkeypatch.setattr(
        manager, "apply_settings_change", lambda *a, **k: {"applied": True}
    )
    sync_config.set_code_sync_enabled(True)
    return tmp_path


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


def test_sync_off_preserves_files_and_unrelated_folder_settings(homes):
    original = skill(homes / ".agents/skills", "original")
    source = skill(homes / "skill-sources", "linked")
    (homes / ".agents/skills/linked").symlink_to(source)
    sync_config.add_sync_folder("Projects")
    sync_config.add_folder_ignore("Projects", "*.log")
    skills_sync.set_enabled(True)
    assert {f.relpath for f in sync_config.sync_folders()} == {
        "Projects",
        ".agents/skills",
        "skill-sources/linked",
    }
    skills_sync.set_enabled(False)
    assert [(f.relpath, f.extra_ignores) for f in sync_config.sync_folders()] == [
        ("Projects", ("*.log",))
    ]
    assert (original / "SKILL.md").read_text() == "original"
    assert (source / "SKILL.md").read_text() == "original"
    assert (homes / ".agents/skills/linked").is_symlink()
    assert not skills_sync.accepts_peer_folder(".agents/skills")
    assert not skills_sync.accepts_peer_folder("skill-sources/linked")
    assert skills_sync.accepts_peer_folder("Projects/other")
    skills_sync.reconcile()
    assert len(sync_config.sync_folders()) == 1
    skills_sync.set_enabled(True)
    assert len(sync_config.sync_folders()) == 3


def test_existing_folder_sync_is_recognized_without_claiming_unrelated_sources(homes):
    source = skill(homes / "Projects/skill-source", "sample")
    (homes / ".agents/skills").mkdir(parents=True)
    (homes / ".agents/skills/sample").symlink_to(source)
    sync_config.add_sync_folder(".agents/skills")
    sync_config.add_sync_folder("Projects")
    assert skills_sync.enabled()
    assert not skills_sync.reconcile()["added"]
    skills_sync.set_enabled(False)
    assert [f.relpath for f in sync_config.sync_folders()] == ["Projects"]


def test_new_linked_sources_are_registered_on_later_ticks(homes):
    skills_sync.set_enabled(True)
    new = skill(homes / "sources", "new")
    (homes / ".agents/skills/new").symlink_to(new)
    assert skills_sync.reconcile()["added"] == ["sources/new"]
    assert skills_sync.reconcile()["added"] == []


@pytest.mark.parametrize("source_root", [".", ".ssh", ".codex/plugins/cache"])
def test_linked_sources_never_share_home_credentials_or_plugin_caches(
    homes, source_root
):
    source = homes / source_root
    source.mkdir(parents=True, exist_ok=True)
    (source / "SKILL.md").write_text("example")
    personal = homes / ".agents/skills"
    personal.mkdir(parents=True)
    (personal / "unsafe-source").symlink_to(source)
    result = skills_sync.set_enabled(True)
    assert [f.relpath for f in sync_config.sync_folders()] == [".agents/skills"]
    assert result["warnings"]


def test_failed_enable_restores_preferences_and_registrations(homes, monkeypatch):
    def fail(*args, **kwargs):
        raise CodeSyncError("transport unavailable")

    monkeypatch.setattr(manager, "apply_settings_change", fail)
    sync_config.add_sync_folder("Projects")
    with pytest.raises(CodeSyncError):
        skills_sync.set_enabled(True)
    assert not skills_sync.enabled()
    assert [f.relpath for f in sync_config.sync_folders()] == ["Projects"]


def test_first_enable_uses_transport_eligibility_and_rolls_back(homes, monkeypatch):
    sync_config.set_code_sync_enabled(False)

    def fail(*args, **kwargs):
        raise CodeSyncError("Add a second machine")

    monkeypatch.setattr(manager, "enable_code_sync", fail)
    with pytest.raises(CodeSyncError, match="second machine"):
        skills_sync.set_enabled(True)
    assert not skills_sync.enabled()
    assert not sync_config.code_sync_enabled()
    assert sync_config.sync_folders() == ()


def test_refuses_future_state_schema(homes):
    skills_sync.STATE_PATH.write_text(json.dumps({"schema_version": 999}))
    with pytest.raises(ValueError, match="update Openbase"):
        skills_sync.enabled()


def test_peer_cannot_reenable_opted_out_skill_sharing(homes, monkeypatch):
    skills_sync.set_enabled(False)
    folder_id = sync_config.folder_id_for_relpath(".agents/skills")
    monkeypatch.setattr(
        manager,
        "SyncthingClient",
        lambda: SimpleNamespace(
            pending_folders=lambda: {
                folder_id: {"offeredBy": {"peer": {"label": ".agents/skills"}}}
            }
        ),
    )
    assert manager.accept_pending_folders() == []
    assert not sync_config.sync_folders()
