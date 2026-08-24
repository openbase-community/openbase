from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import views  # noqa: E402


def _request(method: str, path: str, data: dict | None = None):
    factory = APIRequestFactory()
    request = getattr(factory, method)(path, data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def _write_skill(root: Path, name: str, content: str = "instructions") -> Path:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


def _patch_skill_homes(
    monkeypatch,
    agents_home: Path,
    codex_home: Path,
    claude_home: Path | None = None,
) -> None:
    from openbase_coder_cli import skills_autolink

    resolved_claude_home = claude_home or agents_home.parent / "claude"
    monkeypatch.setattr(
        views._skills, "_home_skills_dir", lambda: agents_home / "skills"
    )
    monkeypatch.setattr(views, "_home_skills_dir", lambda: agents_home / "skills")
    monkeypatch.setattr(views, "CODEX_HOME_DIR", codex_home)
    monkeypatch.setattr(views._skills, "CODEX_HOME_DIR", codex_home)
    monkeypatch.setattr(views, "CLAUDE_CONFIG_DIR", resolved_claude_home)
    monkeypatch.setattr(views._skills, "CLAUDE_CONFIG_DIR", resolved_claude_home)
    monkeypatch.setattr(
        skills_autolink, "home_skills_dir", lambda: agents_home / "skills"
    )
    monkeypatch.setattr(skills_autolink, "CODEX_HOME_DIR", codex_home)
    monkeypatch.setattr(skills_autolink, "CLAUDE_CONFIG_DIR", resolved_claude_home)
    monkeypatch.setattr(
        views._skills.dispatcher_config,
        "CODEX_DISPATCHER_CONFIG_PATH",
        agents_home.parent / "dispatcher-config.json",
    )


def test_skills_list_covers_shared_agent_homes(tmp_path: Path, monkeypatch):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    claude_home = tmp_path / "claude-home"
    _write_skill(agents_home, "home-skill")
    _write_skill(codex_home, "codex-skill")
    _write_skill(claude_home, "claude-skill")

    _patch_skill_homes(monkeypatch, agents_home, codex_home, claude_home)

    response = views.skills_list(_request("get", "/api/skills/"))

    assert response.status_code == 200
    sections = {section["key"]: section for section in response.data["sections"]}
    assert set(sections) == {"home", "codex", "claude"}
    assert sections["home"]["label"] == "Personal Codex skills"
    assert sections["home"]["skills_dir"] == str(agents_home / "skills")
    assert [skill["name"] for skill in sections["home"]["skills"]] == ["home-skill"]
    assert sections["codex"]["label"] == "Codex skills"
    assert sections["codex"]["skills_dir"] == str(codex_home / "skills")
    assert [skill["name"] for skill in sections["codex"]["skills"]] == ["codex-skill"]
    assert sections["claude"]["label"] == "Claude Code skills"
    assert sections["claude"]["skills_dir"] == str(claude_home / "skills")
    assert [skill["name"] for skill in sections["claude"]["skills"]] == ["claude-skill"]
    assert response.data["skills_dir"] == str(agents_home / "skills")
    assert [skill["name"] for skill in response.data["skills"]] == ["home-skill"]


def test_skills_symlink_links_home_skill_to_codex(tmp_path: Path, monkeypatch):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    source_dir = _write_skill(agents_home, "shared-skill")

    _patch_skill_homes(monkeypatch, agents_home, codex_home)

    response = views.skills_symlink(
        _request(
            "post",
            "/api/skills/symlink/",
            {
                "name": "shared-skill",
                "source_scope": "home",
                "target_scope": "codex",
            },
        )
    )

    target_dir = codex_home / "skills" / "shared-skill"
    assert response.status_code == 201
    assert response.data["created"] is True
    assert response.data["source_dir"] == str(source_dir.resolve())
    assert target_dir.is_symlink()
    assert target_dir.resolve() == source_dir.resolve()
    assert (target_dir / "SKILL.md").read_text(encoding="utf-8") == "instructions"


def test_skills_symlink_links_home_skill_to_claude(tmp_path: Path, monkeypatch):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    claude_home = tmp_path / "claude-home"
    source_dir = _write_skill(agents_home, "shared-skill")

    _patch_skill_homes(monkeypatch, agents_home, codex_home, claude_home)

    response = views.skills_symlink(
        _request(
            "post",
            "/api/skills/symlink/",
            {
                "name": "shared-skill",
                "source_scope": "home",
                "target_scope": "claude",
            },
        )
    )

    target_dir = claude_home / "skills" / "shared-skill"
    assert response.status_code == 201
    assert response.data["created"] is True
    assert response.data["target_scope"] == "claude"
    assert target_dir.is_symlink()
    assert target_dir.resolve() == source_dir.resolve()


def test_skills_symlink_links_claude_skill_to_codex(tmp_path: Path, monkeypatch):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    claude_home = tmp_path / "claude-home"
    source_dir = _write_skill(claude_home, "shared-claude-skill")

    _patch_skill_homes(monkeypatch, agents_home, codex_home, claude_home)

    response = views.skills_symlink(
        _request(
            "post",
            "/api/skills/symlink/",
            {
                "name": "shared-claude-skill",
                "source_scope": "claude",
                "target_scope": "codex",
            },
        )
    )

    target_dir = codex_home / "skills" / "shared-claude-skill"
    assert response.status_code == 201
    assert response.data["created"] is True
    assert response.data["source_scope"] == "claude"
    assert target_dir.is_symlink()
    assert target_dir.resolve() == source_dir.resolve()


def test_skills_symlink_returns_ok_when_link_already_exists(
    tmp_path: Path, monkeypatch
):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    source_dir = _write_skill(agents_home, "shared-skill")
    target_dir = codex_home / "skills" / "shared-skill"
    target_dir.parent.mkdir(parents=True)
    target_dir.symlink_to(source_dir, target_is_directory=True)

    _patch_skill_homes(monkeypatch, agents_home, codex_home)

    response = views.skills_symlink(
        _request(
            "post",
            "/api/skills/symlink/",
            {
                "name": "shared-skill",
                "source_scope": "home",
                "target_scope": "codex",
            },
        )
    )

    assert response.status_code == 200
    assert response.data["created"] is False


def test_skills_symlink_rejects_existing_non_link_target(tmp_path: Path, monkeypatch):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    _write_skill(agents_home, "shared-skill", "home")
    _write_skill(codex_home, "shared-skill", "codex")

    _patch_skill_homes(monkeypatch, agents_home, codex_home)

    response = views.skills_symlink(
        _request(
            "post",
            "/api/skills/symlink/",
            {
                "name": "shared-skill",
                "source_scope": "home",
                "target_scope": "codex",
            },
        )
    )

    assert response.status_code == 409
    assert "already exists" in response.data["error"]
    assert not (codex_home / "skills" / "shared-skill").is_symlink()


def test_skills_auto_link_setting_defaults_disabled(tmp_path: Path, monkeypatch):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    config_path = tmp_path / "dispatcher-config.json"
    _write_skill(agents_home, "shared-skill")
    _patch_skill_homes(monkeypatch, agents_home, codex_home)
    monkeypatch.setattr(
        views._skills.dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path
    )

    response = views.skills_list(_request("get", "/api/skills/"))

    assert response.status_code == 200
    assert (
        response.data["auto_link_personal_skills"]["auto_link_personal_skills"] is False
    )
    assert response.data["auto_link_personal_skills_sync"] is None
    assert not (codex_home / "skills" / "shared-skill").exists()


def test_skills_auto_link_setting_enables_and_links_personal_skills(
    tmp_path: Path, monkeypatch
):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    claude_home = tmp_path / "claude-home"
    config_path = tmp_path / "dispatcher-config.json"
    source_dir = _write_skill(agents_home, "shared-skill")
    claude_source_dir = _write_skill(claude_home, "shared-claude-skill")
    _patch_skill_homes(monkeypatch, agents_home, codex_home, claude_home)
    monkeypatch.setattr(
        views._skills.dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path
    )

    response = views.skills_auto_link_settings(
        _request(
            "patch",
            "/api/skills/auto-link-personal/",
            {"auto_link_personal_skills": True},
        )
    )

    codex_target_dir = codex_home / "skills" / "shared-skill"
    claude_target_dir = claude_home / "skills" / "shared-skill"
    codex_claude_target_dir = codex_home / "skills" / "shared-claude-skill"
    assert response.status_code == 200
    assert response.data["auto_link_personal_skills"] is True
    assert response.data["codex_skills_dir"] == str(codex_home / "skills")
    assert response.data["claude_skills_dir"] == str(claude_home / "skills")
    # home skill -> codex + claude, claude skill -> codex; the claude home is
    # never linked back onto itself.
    assert response.data["sync"]["created"] == 3
    assert response.data["sync"]["conflicts"] == 0
    assert codex_target_dir.is_symlink()
    assert codex_target_dir.resolve() == source_dir.resolve()
    assert claude_target_dir.is_symlink()
    assert claude_target_dir.resolve() == source_dir.resolve()
    assert codex_claude_target_dir.is_symlink()
    assert codex_claude_target_dir.resolve() == claude_source_dir.resolve()

    list_response = views.skills_list(_request("get", "/api/skills/"))
    sync = list_response.data["auto_link_personal_skills_sync"]
    assert sync["created"] == 0
    # The home skill linked into the claude home now also shows up as a
    # claude-source skill, so the re-sync sees four already-linked entries.
    assert sync["already_linked"] == 4


def test_skills_auto_link_reports_conflict_without_overwriting(
    tmp_path: Path, monkeypatch
):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    config_path = tmp_path / "dispatcher-config.json"
    _write_skill(agents_home, "shared-skill", "home")
    _write_skill(codex_home, "shared-skill", "codex")
    _patch_skill_homes(monkeypatch, agents_home, codex_home)
    monkeypatch.setattr(
        views._skills.dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config_path
    )

    response = views.skills_auto_link_settings(
        _request(
            "patch",
            "/api/skills/auto-link-personal/",
            {"auto_link_personal_skills": True},
        )
    )

    assert response.status_code == 200
    # home -> codex conflicts, home -> claude links; the fresh claude link is
    # then re-seen as a claude source whose codex target is still the
    # conflicting real directory.
    assert response.data["sync"]["conflicts"] == 2
    assert response.data["sync"]["results"][0]["status"] == "conflict"
    assert response.data["sync"]["results"][0]["target_scope"] == "codex"
    target_dir = codex_home / "skills" / "shared-skill"
    assert not target_dir.is_symlink()
    assert (target_dir / "SKILL.md").read_text(encoding="utf-8") == "codex"


def test_skill_delete_unlinks_symlink_without_deleting_source(
    tmp_path: Path, monkeypatch
):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    source_dir = _write_skill(agents_home, "shared-skill")
    target_dir = codex_home / "skills" / "shared-skill"
    target_dir.parent.mkdir(parents=True)
    target_dir.symlink_to(source_dir, target_is_directory=True)

    _patch_skill_homes(monkeypatch, agents_home, codex_home)

    request = _request(
        "delete",
        "/api/skills/shared-skill/?scope=codex",
    )
    response = views.skill_detail(request, "shared-skill")

    assert response.status_code == 200
    assert not target_dir.exists()
    assert source_dir.is_dir()
    assert (source_dir / "SKILL.md").read_text(encoding="utf-8") == "instructions"


def test_skills_symlink_preserves_existing_source_symlink_chain(
    tmp_path: Path, monkeypatch
):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    real_skill = tmp_path / "Developer" / "skills" / "shared-skill"
    real_skill.mkdir(parents=True)
    (real_skill / "SKILL.md").write_text("real instructions", encoding="utf-8")
    source_dir = agents_home / "skills" / "shared-skill"
    source_dir.parent.mkdir(parents=True)
    source_dir.symlink_to(real_skill, target_is_directory=True)
    _patch_skill_homes(monkeypatch, agents_home, codex_home)

    response = views.skills_symlink(
        _request(
            "post",
            "/api/skills/symlink/",
            {
                "name": "shared-skill",
                "source_scope": "home",
                "target_scope": "codex",
            },
        )
    )

    target_dir = codex_home / "skills" / "shared-skill"
    assert response.status_code == 201
    assert response.data["source_dir"] == str(source_dir)
    assert target_dir.is_symlink()
    assert os.readlink(target_dir) == str(source_dir)
    assert target_dir.resolve() == real_skill.resolve()


def _printing_press_registry() -> dict:
    return {
        "schema_version": 2,
        "entries": [
            {
                "name": "demo",
                "category": "developer-tools",
                "api": "Demo API",
                "description": "Demo skill for testing installs.",
                "search_terms": ["demo", "testing", "install"],
                "path": "library/developer-tools/demo",
                "release": {
                    "cli_name": "demo-pp-cli",
                    "version": "2026.6.1",
                    "released_at": "2026-06-21T00:00:00Z",
                },
                "printer": "printer",
                "printer_name": "Printer Name",
                "creator": {"handle": "printer", "name": "Printer Name"},
                "mcp": {
                    "binary": "demo-pp-mcp",
                    "transports": ["stdio"],
                    "tool_count": 2,
                    "public_tool_count": 1,
                    "auth_type": "api_key",
                    "env_vars": ["DEMO_API_KEY"],
                    "mcp_ready": "full",
                    "spec_format": "openapi3",
                },
            },
            {
                "name": "billing",
                "category": "accounting",
                "api": "Billing API",
                "description": "Billing workflows.",
                "search_terms": ["invoice"],
                "release": {"cli_name": "billing-pp-cli", "version": "2026.6.2"},
            },
        ],
    }


def test_printing_press_catalog_filters_and_reports_install_status(
    tmp_path: Path, monkeypatch
):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    claude_home = tmp_path / "claude-home"
    _write_skill(agents_home, "pp-demo")
    _patch_skill_homes(monkeypatch, agents_home, codex_home, claude_home)
    monkeypatch.setattr(
        views._skills, "_fetch_printing_press_registry", _printing_press_registry
    )

    response = views.printing_press_catalog(
        _request("get", "/api/skills/printing-press/catalog/?q=test")
    )

    assert response.status_code == 200
    assert response.data["schema_version"] == 2
    assert response.data["categories"] == [
        {"name": "accounting", "count": 1},
        {"name": "developer-tools", "count": 1},
    ]
    assert [entry["name"] for entry in response.data["entries"]] == ["demo"]
    assert response.data["entries"][0]["skill_name"] == "pp-demo"
    assert response.data["entries"][0]["installed_targets"] == {
        "claude": False,
        "codex": False,
        "home": True,
    }
    assert response.data["entries"][0]["mcp"]["auth_type"] == "api_key"


def test_printing_press_install_writes_skill_to_selected_targets(
    tmp_path: Path, monkeypatch
):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    claude_home = tmp_path / "claude-home"
    _patch_skill_homes(monkeypatch, agents_home, codex_home, claude_home)
    monkeypatch.setattr(
        views._skills, "_fetch_printing_press_registry", _printing_press_registry
    )
    monkeypatch.setattr(
        views._skills,
        "_fetch_printing_press_skill_content",
        lambda name: f"---\nname: pp-{name}\n---\n\ninstructions",
    )

    response = views.printing_press_install(
        _request(
            "post",
            "/api/skills/printing-press/install/",
            {"name": "demo", "targets": ["home", "claude"]},
        )
    )

    assert response.status_code == 201
    assert response.data["skill_name"] == "pp-demo"
    assert [result["status"] for result in response.data["results"]] == [
        "installed",
        "installed",
    ]
    assert (
        (agents_home / "skills" / "pp-demo" / "SKILL.md")
        .read_text(encoding="utf-8")
        .startswith("---\nname: pp-demo")
    )
    assert (claude_home / "skills" / "pp-demo" / "SKILL.md").is_file()
    assert not (codex_home / "skills" / "pp-demo" / "SKILL.md").exists()


def test_printing_press_install_is_idempotent_without_refetching(
    tmp_path: Path, monkeypatch
):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    _write_skill(agents_home, "pp-demo")
    _patch_skill_homes(monkeypatch, agents_home, codex_home)
    monkeypatch.setattr(
        views._skills, "_fetch_printing_press_registry", _printing_press_registry
    )

    def fail_fetch(name: str) -> str:
        raise AssertionError(f"unexpected fetch for {name}")

    monkeypatch.setattr(
        views._skills, "_fetch_printing_press_skill_content", fail_fetch
    )

    response = views.printing_press_install(
        _request(
            "post",
            "/api/skills/printing-press/install/",
            {"name": "demo", "targets": ["home"]},
        )
    )

    assert response.status_code == 200
    assert response.data["results"] == [
        {
            "target": "home",
            "status": "already_installed",
            "path": str(agents_home / "skills" / "pp-demo" / "SKILL.md"),
            "created": False,
        }
    ]


def test_printing_press_install_rejects_invalid_target(tmp_path: Path, monkeypatch):
    _patch_skill_homes(monkeypatch, tmp_path / "agents-home", tmp_path / "codex-home")

    response = views.printing_press_install(
        _request(
            "post",
            "/api/skills/printing-press/install/",
            {"name": "demo", "targets": ["project"]},
        )
    )

    assert response.status_code == 400
    assert response.data["error"] == "invalid target scope"


def test_printing_press_install_rejects_unknown_catalog_name(
    tmp_path: Path, monkeypatch
):
    _patch_skill_homes(monkeypatch, tmp_path / "agents-home", tmp_path / "codex-home")
    monkeypatch.setattr(
        views._skills, "_fetch_printing_press_registry", _printing_press_registry
    )

    response = views.printing_press_install(
        _request(
            "post",
            "/api/skills/printing-press/install/",
            {"name": "missing", "targets": ["home"]},
        )
    )

    assert response.status_code == 404
    assert "was not found" in response.data["error"]


def test_printing_press_install_reports_target_conflict(tmp_path: Path, monkeypatch):
    agents_home = tmp_path / "agents-home"
    codex_home = tmp_path / "codex-home"
    conflict_dir = agents_home / "skills" / "pp-demo"
    conflict_dir.mkdir(parents=True)
    _patch_skill_homes(monkeypatch, agents_home, codex_home)
    monkeypatch.setattr(
        views._skills, "_fetch_printing_press_registry", _printing_press_registry
    )
    monkeypatch.setattr(
        views._skills,
        "_fetch_printing_press_skill_content",
        lambda name: "instructions",
    )

    response = views.printing_press_install(
        _request(
            "post",
            "/api/skills/printing-press/install/",
            {"name": "demo", "targets": ["home"]},
        )
    )

    assert response.status_code == 207
    assert response.data["results"][0]["status"] == "conflict"
