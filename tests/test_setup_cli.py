from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from openbase_coder_cli import claude_auth
from openbase_coder_cli.cli.setup.hooks import session_start_hook_trusted_hash
from openbase_coder_cli.paths import INJECT_SESSION_ID_HOOK_PATH

setup_cli = importlib.import_module("openbase_coder_cli.cli.setup")
codex_home_instructions = importlib.import_module(
    "openbase_coder_cli.codex_home_instructions"
)
_setup_phase_modules = tuple(
    importlib.import_module(f"openbase_coder_cli.cli.setup.{name}")
    for name in ("claude", "codex", "dispatcher", "env", "workspace")
)


def _patch_setup(monkeypatch, name, value):
    """Patch a name on the setup package and every phase module that defines it."""
    for module in (setup_cli, *_setup_phase_modules):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)


def _patch_openbase_agent_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    codex_home = tmp_path / "codex_home"
    claude_config = tmp_path / "claude_config"
    instructions = tmp_path / "openbase" / "instructions"
    _patch_setup(monkeypatch, "CODEX_HOME_DIR", codex_home)
    _patch_setup(monkeypatch, "OPENBASE_CLAUDE_CONFIG_DIR", claude_config)
    monkeypatch.setattr(codex_home_instructions, "CODEX_HOME_DIR", codex_home)
    monkeypatch.setattr(
        codex_home_instructions,
        "OPENBASE_CLAUDE_MD_PATH",
        claude_config / "CLAUDE.md",
    )
    _patch_setup(
        monkeypatch,
        "OPENBASE_CLAUDE_SETTINGS_PATH",
        claude_config / "settings.json",
    )
    _patch_setup(
        monkeypatch,
        "NORMAL_CLAUDE_SETTINGS_PATH",
        tmp_path / "normal_claude" / "settings.json",
    )
    _patch_setup(
        monkeypatch,
        "NORMAL_CLAUDE_CONFIG_DIR",
        tmp_path / "normal_claude",
    )
    _patch_setup(
        monkeypatch,
        "NORMAL_CODEX_AGENTS_MD_PATH",
        tmp_path / "normal_codex" / "AGENTS.md",
    )
    monkeypatch.setattr(
        codex_home_instructions,
        "NORMAL_CODEX_AGENTS_MD_PATH",
        tmp_path / "normal_codex" / "AGENTS.md",
    )
    _patch_setup(
        monkeypatch,
        "CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH",
        instructions / "VOICE_INSTRUCTIONS.md",
    )
    _patch_setup(
        monkeypatch,
        "CODEX_DISPATCHER_INSTRUCTIONS_PATH",
        instructions / "DISPATCHER_INSTRUCTIONS.md",
    )
    _patch_setup(
        monkeypatch,
        "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH",
        instructions / "SUPER_AGENT_INSTRUCTIONS.md",
    )
    return codex_home, claude_config


def _make_workspace_checkout(root):
    (root / "cli").mkdir(parents=True)
    (root / "multi.json").write_text("{}", encoding="utf-8")
    return root


def test_resolve_dev_workspace_dir_prefers_explicit_dir(tmp_path) -> None:
    workspace = _make_workspace_checkout(tmp_path / "workspace")

    assert setup_cli.resolve_dev_workspace_dir(str(workspace)) == str(workspace)


def test_resolve_dev_workspace_dir_rejects_non_workspace_dir(tmp_path) -> None:
    plain_dir = tmp_path / "not-a-workspace"
    plain_dir.mkdir()

    with pytest.raises(Exception, match="does not look like"):
        setup_cli.resolve_dev_workspace_dir(str(plain_dir))


def test_resolve_dev_workspace_dir_uses_recorded_installation(
    tmp_path, monkeypatch
) -> None:
    workspace = _make_workspace_checkout(tmp_path / "recorded")
    from openbase_coder_cli.cli.setup import workspace as workspace_phase

    monkeypatch.setattr(
        workspace_phase.InstallationConfig, "exists", classmethod(lambda cls: True)
    )
    monkeypatch.setattr(
        workspace_phase.InstallationConfig,
        "load",
        classmethod(
            lambda cls: setup_cli.InstallationConfig(workspace_path=str(workspace))
        ),
    )

    assert setup_cli.resolve_dev_workspace_dir(None) == str(workspace)


def test_resolve_dev_workspace_dir_uses_editable_install(tmp_path, monkeypatch) -> None:
    workspace = _make_workspace_checkout(tmp_path / "editable")
    from openbase_coder_cli.cli.setup import workspace as workspace_phase

    monkeypatch.setattr(
        workspace_phase.InstallationConfig, "exists", classmethod(lambda cls: False)
    )

    class FakeDist:
        def read_text(self, name):
            assert name == "direct_url.json"
            return json.dumps(
                {
                    "url": (workspace / "cli").as_uri(),
                    "dir_info": {"editable": True},
                }
            )

    monkeypatch.setattr(workspace_phase, "distribution", lambda _name: FakeDist())

    assert setup_cli.resolve_dev_workspace_dir(None) == str(workspace)


def test_resolve_dev_workspace_dir_errors_without_any_workspace(
    monkeypatch,
) -> None:
    from openbase_coder_cli.cli.setup import workspace as workspace_phase

    monkeypatch.setattr(
        workspace_phase.InstallationConfig, "exists", classmethod(lambda cls: False)
    )

    def missing_dist(_name):
        raise workspace_phase.PackageNotFoundError

    monkeypatch.setattr(workspace_phase, "distribution", missing_dist)

    with pytest.raises(Exception, match="No Openbase Coder workspace found"):
        setup_cli.resolve_dev_workspace_dir(None)


def test_ensure_codex_home_default_files_links_missing_files(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    instructions = workspace / "instructions"
    instructions.mkdir(parents=True)
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    shared_instructions = tmp_path / "openbase" / "instructions"
    targets = (
        ("VOICE_INSTRUCTIONS.md", shared_instructions / "VOICE_INSTRUCTIONS.md"),
        (
            "DISPATCHER_INSTRUCTIONS.md",
            shared_instructions / "DISPATCHER_INSTRUCTIONS.md",
        ),
        (
            "SUPER_AGENT_INSTRUCTIONS.md",
            shared_instructions / "SUPER_AGENT_INSTRUCTIONS.md",
        ),
    )
    for resource_name, _target_path in targets:
        (instructions / resource_name).write_text(
            f"default {resource_name}\n",
            encoding="utf-8",
        )
    _patch_setup(monkeypatch, "CODEX_HOME_DEFAULT_FILES", targets)

    setup_cli._ensure_codex_home_default_files(str(workspace))

    for resource_name, target_path in targets:
        source_path = instructions / resource_name
        assert target_path.is_file()
        assert not target_path.is_symlink()
        assert target_path.read_text(encoding="utf-8") == (
            f"<!-- Generated from {source_path}; edit the source template instead. -->\n\n"
            f"default {resource_name}\n"
        )


def test_ensure_codex_home_default_files_renders_template_files(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    instructions = workspace / "instructions"
    instructions.mkdir(parents=True)
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    shared_instructions = tmp_path / "openbase" / "instructions"
    target = shared_instructions / "SUPER_AGENT_INSTRUCTIONS.md"
    (instructions / "SUPER_AGENT_INSTRUCTIONS.md").write_text(
        'Require "${dangerous_confirmation_phrase}".\n',
        encoding="utf-8",
    )
    _patch_setup(
        monkeypatch,
        "CODEX_HOME_DEFAULT_FILES",
        (("SUPER_AGENT_INSTRUCTIONS.md", target),),
    )
    monkeypatch.setattr(
        "openbase_coder_cli.instruction_templates.get_dangerous_confirmation_phrase",
        lambda: "ship it",
    )

    setup_cli._ensure_codex_home_default_files(str(workspace))

    assert target.is_file()
    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8") == (
        f"<!-- Generated from {instructions / 'SUPER_AGENT_INSTRUCTIONS.md'}; "
        'edit the source template instead. -->\n\nRequire "ship it".\n'
    )


def test_ensure_codex_home_default_files_preserves_custom_existing_files(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    instructions = workspace / "instructions"
    instructions.mkdir(parents=True)
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    shared_instructions = tmp_path / "openbase" / "instructions"
    existing_path = codex_home / "AGENTS.md"
    missing_path = shared_instructions / "VOICE_INSTRUCTIONS.md"
    existing_path.parent.mkdir(parents=True)
    existing_path.write_text("custom instructions\n", encoding="utf-8")
    (instructions / "AGENTS.md").write_text("default agents\n", encoding="utf-8")
    (instructions / "VOICE_INSTRUCTIONS.md").write_text(
        "default voice\n",
        encoding="utf-8",
    )
    _patch_setup(
        monkeypatch,
        "CODEX_HOME_DEFAULT_FILES",
        (
            ("AGENTS.md", existing_path),
            ("VOICE_INSTRUCTIONS.md", missing_path),
        ),
    )

    setup_cli._ensure_codex_home_default_files(str(workspace))

    assert not existing_path.is_symlink()
    updated_agents = existing_path.read_text(encoding="utf-8")
    assert updated_agents == (
        "## Openbase Coder Instructions\n\n"
        f"- These instructions are auto generated from {instructions / 'AGENTS.md'}."
        "\n\n"
        "default agents\n"
    )
    assert missing_path.is_file()
    assert not missing_path.is_symlink()
    assert missing_path.read_text(encoding="utf-8") == (
        f"<!-- Generated from {instructions / 'VOICE_INSTRUCTIONS.md'}; "
        "edit the source template instead. -->\n\n"
        "default voice\n"
    )


def test_ensure_codex_home_default_files_rewrites_matching_agents_file(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    instructions = workspace / "instructions"
    instructions.mkdir(parents=True)
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    target_path = codex_home / "AGENTS.md"
    target_path.parent.mkdir(parents=True)
    (instructions / "AGENTS.md").write_text("default agents\n", encoding="utf-8")
    target_path.write_text("default agents\n", encoding="utf-8")
    _patch_setup(
        monkeypatch,
        "CODEX_HOME_DEFAULT_FILES",
        (("AGENTS.md", target_path),),
    )

    setup_cli._ensure_codex_home_default_files(str(workspace))

    assert not target_path.is_symlink()
    assert target_path.read_text(encoding="utf-8") == (
        "## Openbase Coder Instructions\n\n"
        f"- These instructions are auto generated from {instructions / 'AGENTS.md'}."
        "\n\n"
        "default agents\n"
    )


def test_ensure_codex_home_default_files_converts_stale_agents_symlink(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    instructions = workspace / "instructions"
    stale_instructions = tmp_path / "stale-instructions"
    instructions.mkdir(parents=True)
    stale_instructions.mkdir()
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    target_path = codex_home / "AGENTS.md"
    target_path.parent.mkdir(parents=True)
    (instructions / "AGENTS.md").write_text("default agents\n", encoding="utf-8")
    (stale_instructions / "AGENTS.md").write_text("stale agents\n", encoding="utf-8")
    target_path.symlink_to(stale_instructions / "AGENTS.md")
    _patch_setup(
        monkeypatch,
        "CODEX_HOME_DEFAULT_FILES",
        (("AGENTS.md", target_path),),
    )

    setup_cli._ensure_codex_home_default_files(str(workspace))

    assert not target_path.is_symlink()
    updated = target_path.read_text(encoding="utf-8")
    assert updated == (
        "## Openbase Coder Instructions\n\n"
        f"- These instructions are auto generated from {instructions / 'AGENTS.md'}."
        "\n\n"
        "default agents\n"
    )


def test_ensure_codex_home_default_files_converts_current_agents_symlink(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    instructions = workspace / "instructions"
    instructions.mkdir(parents=True)
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    target_path = codex_home / "AGENTS.md"
    target_path.parent.mkdir(parents=True)
    source_path = instructions / "AGENTS.md"
    source_path.write_text("default agents\n", encoding="utf-8")
    target_path.symlink_to(source_path)
    _patch_setup(
        monkeypatch,
        "CODEX_HOME_DEFAULT_FILES",
        (("AGENTS.md", target_path),),
    )

    setup_cli._ensure_codex_home_default_files(str(workspace))

    assert not target_path.is_symlink()
    assert target_path.read_text(encoding="utf-8") == (
        "## Openbase Coder Instructions\n\n"
        f"- These instructions are auto generated from {source_path}.\n\n"
        "default agents\n"
    )


def test_ensure_codex_home_default_files_honors_excluding_normal_agents(
    tmp_path,
    monkeypatch,
) -> None:
    from openbase_coder_cli.services import console_settings

    workspace = tmp_path / "workspace"
    instructions = workspace / "instructions"
    normal_agents = tmp_path / "normal_codex" / "AGENTS.md"
    instructions.mkdir(parents=True)
    normal_agents.parent.mkdir(parents=True)
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    (instructions / "AGENTS.md").write_text("- Openbase rule\n", encoding="utf-8")
    normal_agents.write_text("- Normal rule\n", encoding="utf-8")
    _patch_setup(monkeypatch, "CODEX_HOME_DEFAULT_FILES", ())
    monkeypatch.setattr(
        console_settings,
        "CONSOLE_SETTINGS_JSON_PATH",
        tmp_path / "console-settings.json",
    )
    console_settings.set_include_normal_codex_agents_in_openbase_agents(False)

    setup_cli._ensure_codex_home_default_files(str(workspace))

    content = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert content == (
        "## Openbase Coder Instructions\n\n"
        f"- These instructions are auto generated from {instructions / 'AGENTS.md'}."
        "\n\n"
        "- Openbase rule\n"
    )
    assert "Non-Openbase Instructions" not in content
    assert "Normal rule" not in content


def test_ensure_codex_home_default_files_replaces_stale_openbase_claude_symlink(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    instructions = workspace / "instructions"
    stale_dir = tmp_path / "stale-openbase-codex"
    instructions.mkdir(parents=True)
    stale_dir.mkdir()
    codex_home, claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    stale_agents = stale_dir / "AGENTS.md"
    stale_agents.write_text("- Stale rule\n", encoding="utf-8")
    (instructions / "AGENTS.md").write_text("- Openbase rule\n", encoding="utf-8")
    claude_md = claude_config / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.symlink_to(stale_agents)
    _patch_setup(monkeypatch, "CODEX_HOME_DEFAULT_FILES", ())

    setup_cli._ensure_codex_home_default_files(str(workspace))

    assert claude_md.is_symlink()
    assert claude_md.resolve() == (codex_home / "AGENTS.md").resolve()
    assert claude_md.readlink() == Path(
        os.path.relpath(codex_home / "AGENTS.md", claude_config)
    )


def test_ensure_codex_home_default_files_skips_missing_sources(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    target_path = codex_home / "AGENTS.md"
    _patch_setup(
        monkeypatch,
        "CODEX_HOME_DEFAULT_FILES",
        (("AGENTS.md", target_path),),
    )

    setup_cli._ensure_codex_home_default_files(str(workspace))

    assert not target_path.exists()


def test_ensure_normal_claude_md_symlink_links_to_normal_codex_agents(
    tmp_path,
    monkeypatch,
) -> None:
    _patch_openbase_agent_paths(monkeypatch, tmp_path)
    normal_agents_path = setup_cli.NORMAL_CODEX_AGENTS_MD_PATH
    normal_agents_path.parent.mkdir(parents=True)
    normal_agents_path.write_text("normal codex\n", encoding="utf-8")

    setup_cli._ensure_normal_claude_md_symlink()

    claude_md_path = setup_cli.NORMAL_CLAUDE_CONFIG_DIR / "CLAUDE.md"
    assert claude_md_path.is_symlink()
    assert claude_md_path.resolve() == normal_agents_path.resolve()


def test_ensure_normal_claude_md_symlink_migrates_existing_claude_file(
    tmp_path,
    monkeypatch,
) -> None:
    _patch_openbase_agent_paths(monkeypatch, tmp_path)
    claude_md_path = setup_cli.NORMAL_CLAUDE_CONFIG_DIR / "CLAUDE.md"
    claude_md_path.parent.mkdir(parents=True)
    claude_md_path.write_text("normal claude\n", encoding="utf-8")

    setup_cli._ensure_normal_claude_md_symlink()

    normal_agents_path = setup_cli.NORMAL_CODEX_AGENTS_MD_PATH
    assert normal_agents_path.read_text(encoding="utf-8") == "normal claude\n"
    assert claude_md_path.is_symlink()
    assert claude_md_path.resolve() == normal_agents_path.resolve()


def test_ensure_normal_claude_md_symlink_backs_up_different_file(
    tmp_path,
    monkeypatch,
) -> None:
    _patch_openbase_agent_paths(monkeypatch, tmp_path)
    normal_agents_path = setup_cli.NORMAL_CODEX_AGENTS_MD_PATH
    claude_md_path = setup_cli.NORMAL_CLAUDE_CONFIG_DIR / "CLAUDE.md"
    normal_agents_path.parent.mkdir(parents=True)
    claude_md_path.parent.mkdir(parents=True)
    normal_agents_path.write_text("normal codex\n", encoding="utf-8")
    claude_md_path.write_text("normal claude\n", encoding="utf-8")

    setup_cli._ensure_normal_claude_md_symlink()

    backups = list(claude_md_path.parent.glob("CLAUDE.md.backup-openbase-coder-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "normal claude\n"
    assert claude_md_path.is_symlink()
    assert claude_md_path.resolve() == normal_agents_path.resolve()


def test_ensure_codex_home_dispatcher_config_creates_default(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    _patch_setup(monkeypatch, "CODEX_DISPATCHER_CONFIG_PATH", config_path)

    setup_cli._ensure_codex_home_dispatcher_config()

    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "backend_models": {
            "claude_code": {
                "dispatcher": "opus",
                "super_agents": "opus",
            },
            "codex": {
                "dispatcher": "gpt-5.5",
                "super_agents": "gpt-5.5",
            },
        },
        "dispatcher_voice_id": "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
        "dispatcher_voice_name": "Jacqueline",
        "dispatcher_reasoning_effort": "low",
        "stt_provider": "openbase_cloud",
        "super_agents_reasoning_effort": "high",
        "tts_provider": "openbase_cloud",
    }


def test_ensure_codex_home_dispatcher_config_preserves_existing(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "{\n"
        '  "dispatcher_reasoning_effort": "medium",\n'
        '  "super_agents_reasoning_effort": "xhigh"\n'
        "}\n",
        encoding="utf-8",
    )
    _patch_setup(monkeypatch, "CODEX_DISPATCHER_CONFIG_PATH", config_path)

    setup_cli._ensure_codex_home_dispatcher_config()

    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "dispatcher_reasoning_effort": "medium",
        "super_agents_reasoning_effort": "xhigh",
    }


def test_ensure_codex_home_dispatcher_config_updates_audio_provider_when_requested(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "{\n"
        '  "dispatcher_reasoning_effort": "medium",\n'
        '  "super_agents_reasoning_effort": "xhigh"\n'
        "}\n",
        encoding="utf-8",
    )
    _patch_setup(monkeypatch, "CODEX_DISPATCHER_CONFIG_PATH", config_path)

    setup_cli._ensure_codex_home_dispatcher_config(audio_provider="local")

    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "dispatcher_reasoning_effort": "medium",
        "dispatcher_voice_id": "af_heart",
        "dispatcher_voice_name": "Heart",
        "stt_provider": "local_mlx_whisper",
        "super_agents_reasoning_effort": "xhigh",
        "tts_provider": "kokoro",
    }


def test_symlink_codex_home_skills_links_workspace_skills(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    skill = workspace / "skills" / "skills" / "sample-skill"
    codex_home, claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Sample\n", encoding="utf-8")

    setup_cli._symlink_codex_home_skills(str(workspace))

    target = codex_home / "skills" / "sample-skill"
    assert target.is_symlink()
    assert target.resolve() == skill.resolve()
    claude_target = claude_config / "skills" / "sample-skill"
    assert claude_target.is_symlink()
    assert claude_target.resolve() == skill.resolve()


def test_symlink_codex_home_skills_replaces_existing_symlink(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    skill = workspace / "skills" / "skills" / "sample-skill"
    stale_skill = tmp_path / "stale-skill"
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    target = codex_home / "skills" / "sample-skill"
    skill.mkdir(parents=True)
    stale_skill.mkdir()
    target.parent.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Sample\n", encoding="utf-8")
    target.symlink_to(stale_skill)

    setup_cli._symlink_codex_home_skills(str(workspace))

    assert target.is_symlink()
    assert target.resolve() == skill.resolve()


def test_symlink_codex_home_skills_preserves_real_directories(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    skill = workspace / "skills" / "skills" / "sample-skill"
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    target = codex_home / "skills" / "sample-skill"
    skill.mkdir(parents=True)
    target.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Sample\n", encoding="utf-8")
    (target / "SKILL.md").write_text("# Custom\n", encoding="utf-8")

    setup_cli._symlink_codex_home_skills(str(workspace))

    assert not target.is_symlink()
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# Custom\n"


def _expected_session_id_hook_suffix(codex_home: Path) -> str:
    hook_command = str(INJECT_SESSION_ID_HOOK_PATH)
    state_key = f"{codex_home.resolve() / 'config.toml'}:session_start:0:0"
    return (
        "\n"
        "[[hooks.SessionStart]]\n"
        "\n"
        "[[hooks.SessionStart.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(hook_command)}\n"
        "\n"
        f'[hooks.state."{state_key}"]\n'
        f"trusted_hash = {json.dumps(session_start_hook_trusted_hash(hook_command))}\n"
        "enabled = true\n"
    )


def test_ensure_codex_home_config_creates_config(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    command = workspace / ".venv" / "bin" / "super-agents-mcp"
    codex_home = tmp_path / "codex_home"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    _patch_setup(monkeypatch, "CODEX_HOME_DIR", codex_home)

    setup_cli._ensure_codex_home_config(str(workspace))

    assert (codex_home / "config.toml").read_text(encoding="utf-8") == (
        'sandbox_mode = "danger-full-access"\n'
        "approval_policy = { granular = { sandbox_approval = false, rules = false, "
        "mcp_elicitations = false, request_permissions = false, "
        "skill_approval = false } }\n"
        'model = "gpt-5.5"\n'
        "\n"
        "[mcp_servers.super-agents]\n"
        f"command = {json.dumps(str(command))}\n"
        + _expected_session_id_hook_suffix(codex_home)
    )


def test_ensure_codex_home_config_replaces_stale_values(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    command = workspace / ".venv" / "bin" / "super-agents-mcp"
    codex_home = tmp_path / "codex_home"
    config_path = codex_home / "config.toml"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                'sandbox_mode = "workspace-write"',
                'approval_policy = "on-request"',
                "",
                '[projects."/Users/gabemontague"]',
                'trust_level = "trusted"',
                "",
                "[mcp_servers.super-agents]",
                'command = "/Users/gabemontague/.local/bin/uv"',
                'args = ["--directory", "/bad", "run", "super-agents-mcp"]',
                "",
                "[mcp_servers.playwright]",
                'command = "npx"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    _patch_setup(monkeypatch, "CODEX_HOME_DIR", codex_home)

    setup_cli._ensure_codex_home_config(str(workspace))

    updated = config_path.read_text(encoding="utf-8")
    assert 'sandbox_mode = "workspace-write"' not in updated
    assert 'approval_policy = "on-request"' not in updated
    assert 'sandbox_mode = "danger-full-access"' in updated
    assert (
        "approval_policy = { granular = { sandbox_approval = false, rules = false, "
        "mcp_elicitations = false, request_permissions = false, "
        "skill_approval = false } }"
    ) in updated
    assert updated.count("[mcp_servers.super-agents]") == 1
    assert "/Users/gabemontague/.local/bin/uv" not in updated
    assert "args =" not in updated
    assert '[projects."/Users/gabemontague"]\ntrust_level = "trusted"' in updated
    assert f"command = {json.dumps(str(command))}" in updated
    assert '[mcp_servers.playwright]\ncommand = "npx"' in updated


def test_ensure_codex_home_config_falls_back_to_resolved_uv(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    cli_dir = workspace / "cli"
    codex_home = tmp_path / "codex_home"
    uv_bin = tmp_path / "homebrew" / "bin" / "uv"
    cli_dir.mkdir(parents=True)
    uv_bin.parent.mkdir(parents=True)
    uv_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    _patch_setup(monkeypatch, "CODEX_HOME_DIR", codex_home)
    _patch_setup(
        monkeypatch,
        "which",
        lambda command: str(uv_bin) if command == "uv" else None,
    )

    setup_cli._ensure_codex_home_config(str(workspace))

    assert (codex_home / "config.toml").read_text(encoding="utf-8") == (
        'sandbox_mode = "danger-full-access"\n'
        "approval_policy = { granular = { sandbox_approval = false, rules = false, "
        "mcp_elicitations = false, request_permissions = false, "
        "skill_approval = false } }\n"
        'model = "gpt-5.5"\n'
        "\n"
        "[mcp_servers.super-agents]\n"
        f"command = {json.dumps(str(uv_bin))}\n"
        f"args = {json.dumps(['--directory', str(cli_dir), 'run', 'super-agents-mcp'])}\n"
        + _expected_session_id_hook_suffix(codex_home)
    )


def test_super_agents_mcp_command_prefers_packaged_python_bin(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    package = tmp_path / "package"
    python_path = package / "python" / "bin" / "python"
    command = python_path.parent / "super-agents-mcp"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    _patch_setup(
        monkeypatch,
        "current_runtime_package",
        lambda: SimpleNamespace(python_path=python_path),
    )
    _patch_setup(monkeypatch, "which", lambda _command: None)

    command_path, args = setup_cli._super_agents_mcp_command(workspace)

    assert command_path == command
    assert args == []


def test_ensure_claude_config_installs_super_agents_mcp(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    command = workspace / ".venv" / "bin" / "super-agents-mcp"
    dispatcher_config = tmp_path / "dispatcher-config.json"
    _codex_home, claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    claude_json = claude_config / ".claude.json"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    claude_json.parent.mkdir(parents=True)
    claude_json.write_text(
        json.dumps(
            {
                "firstStartTime": "2026-06-18T00:00:00.000Z",
                "mcpServers": {"playwright": {"command": "npx"}},
            }
        ),
        encoding="utf-8",
    )
    _patch_setup(monkeypatch, "OPENBASE_CLAUDE_JSON_PATH", claude_json)
    _patch_setup(monkeypatch, "CODEX_DISPATCHER_CONFIG_PATH", dispatcher_config)

    setup_cli._ensure_claude_config(str(workspace))

    payload = json.loads(claude_json.read_text(encoding="utf-8"))
    assert payload["firstStartTime"] == "2026-06-18T00:00:00.000Z"
    assert payload["mcpServers"]["playwright"] == {"command": "npx"}
    assert payload["mcpServers"]["super-agents"] == {
        "type": "stdio",
        "command": str(command),
        "env": {
            "CLAUDE_CONFIG_DIR": str(claude_config),
            "SUPER_AGENTS_DEFAULT_CONFIG_PATH": str(dispatcher_config),
            "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH": str(
                setup_cli.CODEX_SUPER_AGENT_INSTRUCTIONS_PATH
            ),
        },
    }
    settings = json.loads((claude_config / "settings.json").read_text(encoding="utf-8"))
    assert settings["permissions"]["defaultMode"] == "bypassPermissions"
    assert settings["skipDangerousModePermissionPrompt"] is True
    assert settings["skipAutoPermissionPrompt"] is True
    assert settings["claudeMdExcludes"] == [
        str(setup_cli.NORMAL_CLAUDE_CONFIG_DIR / "CLAUDE.md")
    ]


def test_ensure_claude_settings_seeds_from_normal_claude_settings(
    tmp_path,
    monkeypatch,
) -> None:
    _codex_home, claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    normal_settings = setup_cli.NORMAL_CLAUDE_SETTINGS_PATH
    normal_settings.parent.mkdir(parents=True)
    normal_settings.write_text(
        json.dumps(
            {
                "model": "sonnet",
                "theme": "light",
                "permissions": {
                    "allow": ["Bash(git status:*)"],
                    "deny": [],
                    "defaultMode": "auto",
                },
                "skipDangerousModePermissionPrompt": False,
                "skipAutoPermissionPrompt": False,
                "claudeMdExcludes": ["/tmp/other-team/CLAUDE.md"],
            }
        ),
        encoding="utf-8",
    )

    setup_cli._ensure_claude_settings()

    settings = json.loads((claude_config / "settings.json").read_text(encoding="utf-8"))
    assert settings["model"] == "sonnet"
    assert settings["theme"] == "light"
    assert settings["permissions"] == {
        "allow": ["Bash(git status:*)"],
        "deny": [],
        "defaultMode": "bypassPermissions",
    }
    assert settings["skipDangerousModePermissionPrompt"] is True
    assert settings["skipAutoPermissionPrompt"] is True
    assert settings["claudeMdExcludes"] == [
        "/tmp/other-team/CLAUDE.md",
        str(setup_cli.NORMAL_CLAUDE_CONFIG_DIR / "CLAUDE.md"),
    ]
    assert settings["hooks"]["SessionStart"] == [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": str(INJECT_SESSION_ID_HOOK_PATH)}],
        }
    ]


def test_ensure_claude_auth_bridge_runs_login_when_requested(monkeypatch) -> None:
    _patch_setup(monkeypatch, "copy_normal_claude_keychain", lambda: False)
    statuses = iter(
        [
            claude_auth.ClaudeAuthStatus(
                logged_in=False, raw_output="{}", returncode=0
            ),
            claude_auth.ClaudeAuthStatus(logged_in=True, raw_output="{}", returncode=0),
        ]
    )
    login_calls = []
    _patch_setup(monkeypatch, "claude_auth_status", lambda: next(statuses))
    _patch_setup(
        monkeypatch,
        "sync_normal_claude_state",
        lambda: claude_auth.ClaudeAuthBridgeResult(
            state_updated=False,
            message="already synced",
        ),
    )
    _patch_setup(
        monkeypatch,
        "run_claude_login",
        lambda: login_calls.append(True) or 0,
    )

    setup_cli._ensure_claude_auth_bridge(login_if_needed=True)

    assert login_calls == [True]


def test_ensure_claude_auth_bridge_syncs_before_status(monkeypatch) -> None:
    calls = []
    _patch_setup(
        monkeypatch,
        "sync_normal_claude_state",
        lambda: (
            calls.append("sync")
            or claude_auth.ClaudeAuthBridgeResult(
                state_updated=True,
                message="synced normal state",
            )
        ),
    )
    _patch_setup(
        monkeypatch,
        "copy_normal_claude_keychain",
        lambda: calls.append("copy-keychain") or True,
    )
    _patch_setup(
        monkeypatch,
        "claude_auth_status",
        lambda: (
            calls.append("status")
            or claude_auth.ClaudeAuthStatus(
                logged_in=True, raw_output="{}", returncode=0
            )
        ),
    )

    setup_cli._ensure_claude_auth_bridge(login_if_needed=False)

    assert calls == ["sync", "copy-keychain", "status"]


def test_ensure_claude_auth_bridge_does_not_login_unless_requested(monkeypatch) -> None:
    _patch_setup(monkeypatch, "copy_normal_claude_keychain", lambda: False)
    login_calls = []
    _patch_setup(
        monkeypatch,
        "claude_auth_status",
        lambda: claude_auth.ClaudeAuthStatus(
            logged_in=False,
            raw_output="{}",
            returncode=0,
        ),
    )
    _patch_setup(
        monkeypatch,
        "sync_normal_claude_state",
        lambda: claude_auth.ClaudeAuthBridgeResult(
            state_updated=False,
            message="already synced",
        ),
    )
    _patch_setup(
        monkeypatch,
        "run_claude_login",
        lambda: login_calls.append(True) or 0,
    )

    setup_cli._ensure_claude_auth_bridge(login_if_needed=False)

    assert login_calls == []


def test_selected_coding_backend_reads_existing_env(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=claude_code\n", encoding="utf-8")

    assert setup_cli._selected_coding_backend(env_file, None) == "claude_code"


def test_symlink_codex_auth_links_before_codex_login(tmp_path, monkeypatch) -> None:
    """The service auth symlink dangles until `codex login` writes auth.json."""
    codex_module = importlib.import_module("openbase_coder_cli.cli.setup.codex")
    codex_home = tmp_path / "openbase" / "codex_home"
    monkeypatch.setattr(codex_module, "CODEX_HOME_DIR", codex_home)
    monkeypatch.setattr(codex_module.Path, "home", classmethod(lambda cls: tmp_path))

    setup_cli._symlink_codex_auth()

    service_auth = codex_home / "auth.json"
    assert service_auth.is_symlink()
    assert not service_auth.is_file()

    normal_auth = tmp_path / ".codex" / "auth.json"
    normal_auth.parent.mkdir(parents=True)
    normal_auth.write_text('{"tokens": {}}', encoding="utf-8")
    assert service_auth.is_file()

    # Re-running keeps the existing link.
    setup_cli._symlink_codex_auth()
    assert service_auth.resolve() == normal_auth.resolve()


def test_ensure_codex_home_config_can_link_normal_codex_config(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "custom-workspace"
    command = workspace / "cli" / ".venv" / "bin" / "super-agents-mcp"
    codex_home = tmp_path / "openbase" / "codex_home"
    normal_config = tmp_path / "codex" / "config.toml"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    normal_config.parent.mkdir(parents=True)
    normal_config.write_text(
        "\n".join(
            [
                '[projects."/repo"]',
                'trust_level = "trusted"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    _patch_setup(monkeypatch, "CODEX_HOME_DIR", codex_home)
    _patch_setup(monkeypatch, "NORMAL_CODEX_CONFIG_PATH", normal_config)

    setup_cli._ensure_codex_home_config(
        str(workspace),
        link_codex_config=True,
    )

    service_config = codex_home / "config.toml"
    updated = normal_config.read_text(encoding="utf-8")
    assert service_config.is_symlink()
    assert service_config.resolve() == normal_config.resolve()
    assert service_config.read_text(encoding="utf-8") == updated
    assert f"command = {json.dumps(str(command))}" in updated
    assert '[projects."/repo"]\ntrust_level = "trusted"' in updated


def test_symlink_codex_home_config_preserves_existing_service_config(
    tmp_path, monkeypatch
) -> None:
    codex_home = tmp_path / "openbase" / "codex_home"
    service_config = codex_home / "config.toml"
    normal_config = tmp_path / "codex" / "config.toml"
    service_config.parent.mkdir(parents=True)
    service_config.write_text('sandbox_mode = "danger-full-access"\n', encoding="utf-8")
    _patch_setup(monkeypatch, "CODEX_HOME_DIR", codex_home)
    _patch_setup(monkeypatch, "NORMAL_CODEX_CONFIG_PATH", normal_config)

    setup_cli._symlink_codex_home_config()

    assert service_config.is_symlink()
    assert service_config.resolve() == normal_config.resolve()
    assert normal_config.read_text(encoding="utf-8") == (
        'sandbox_mode = "danger-full-access"\n'
    )


def test_ensure_env_file_documents_coding_backend_default(tmp_path) -> None:
    env_file = tmp_path / ".env"

    setup_cli._ensure_env_file(
        str(env_file),
        assembly_ai_api_key="",
        cartesia_api_key="",
    )

    content = env_file.read_text(encoding="utf-8")
    assert "OPENBASE_CODING_BACKEND=codex" in content
    assert "# openbase_cloud runs Cloud-proxied Claude Code" in content
    assert "CODEX_CLAUDE_" not in content
    assert "SUPER_AGENTS_CLAUDE_TUI_CMD" not in content
    assert "CLAUDE_CONFIG_DIR=" in content
    assert "SUPER_AGENTS_DEFAULT_CONFIG_PATH=" in content
    assert "CODEX_MODEL=" not in content


def test_ensure_env_file_can_select_backend(tmp_path) -> None:
    env_file = tmp_path / ".env"

    setup_cli._ensure_env_file(
        str(env_file),
        assembly_ai_api_key="",
        cartesia_api_key="",
        coding_backend="openbase-cloud",
    )

    assert "OPENBASE_CODING_BACKEND=openbase_cloud" in env_file.read_text(
        encoding="utf-8"
    )


def test_ensure_openbase_cloud_machine_token_uses_env_backend_url(
    tmp_path,
    monkeypatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENBASE_CODER_CLI_WEB_BACKEND_URL=https://backend.example\n",
        encoding="utf-8",
    )
    calls = []

    class FakeTokenManager:
        def __init__(self, web_backend_url):
            self.web_backend_url = web_backend_url
            self.has_refresh_token = True

    class FakeMachineTokenManager:
        def __init__(self, web_backend_url, token_manager):
            calls.append((web_backend_url, token_manager.web_backend_url))

        def get_machine_token(self):
            calls.append("minted")
            return "obmt_token"

    _patch_setup(monkeypatch, "TokenManager", FakeTokenManager)
    _patch_setup(monkeypatch, "MachineTokenManager", FakeMachineTokenManager)

    setup_cli._ensure_openbase_cloud_machine_token(env_file)

    assert calls == [("https://backend.example", "https://backend.example"), "minted"]


def test_ensure_env_file_updates_existing_backend_only_when_requested(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP_ME=1\nOPENBASE_CODEX_BACKEND=codex\n", encoding="utf-8")

    setup_cli._ensure_env_file(
        str(env_file),
        assembly_ai_api_key="",
        cartesia_api_key="",
    )
    assert "OPENBASE_CODEX_BACKEND=codex" in env_file.read_text(encoding="utf-8")

    setup_cli._ensure_env_file(
        str(env_file),
        assembly_ai_api_key="",
        cartesia_api_key="",
        coding_backend="claude-code",
    )

    content = env_file.read_text(encoding="utf-8")
    assert "KEEP_ME=1" in content
    assert "OPENBASE_CODEX_BACKEND=codex" in content
    assert "OPENBASE_CODING_BACKEND=claude_code" in content


def test_ensure_thread_sync_exchange_dir_creates_syncthing_files(
    tmp_path, monkeypatch
) -> None:
    openbase_dir = tmp_path / "openbase"
    global_ignore = tmp_path / "syncthing" / "global.stignore"
    _patch_setup(monkeypatch, "OPENBASE_BASE_DIR", openbase_dir)
    _patch_setup(
        monkeypatch,
        "_syncthing_global_ignore_path",
        lambda: global_ignore,
    )

    setup_cli._ensure_thread_sync_exchange_dir()

    exchange_dir = openbase_dir / "thread-sync"
    assert exchange_dir.is_dir()
    assert (
        exchange_dir / ".stfolder" / setup_cli.THREAD_SYNC_MARKER_FILE_NAME
    ).is_file()
    assert (exchange_dir / ".stignore").read_text(encoding="utf-8") == (
        "#include .stglobalignore\n"
    )
    assert global_ignore.read_text(encoding="utf-8") == "(?d).DS_Store\n"
    assert (exchange_dir / ".stglobalignore").is_symlink()
    assert (exchange_dir / ".stglobalignore").resolve() == global_ignore.resolve()


def test_ensure_thread_sync_exchange_dir_replaces_stale_global_ignore_symlink(
    tmp_path, monkeypatch
) -> None:
    openbase_dir = tmp_path / "openbase"
    exchange_dir = openbase_dir / "thread-sync"
    stale_global_ignore = tmp_path / "stale" / "global.stignore"
    global_ignore = tmp_path / "syncthing" / "global.stignore"
    exchange_dir.mkdir(parents=True)
    stale_global_ignore.parent.mkdir()
    stale_global_ignore.write_text("stale\n", encoding="utf-8")
    (exchange_dir / ".stglobalignore").symlink_to(stale_global_ignore)
    _patch_setup(monkeypatch, "OPENBASE_BASE_DIR", openbase_dir)
    _patch_setup(
        monkeypatch,
        "_syncthing_global_ignore_path",
        lambda: global_ignore,
    )

    setup_cli._ensure_thread_sync_exchange_dir()

    assert (exchange_dir / ".stglobalignore").resolve() == global_ignore.resolve()


def test_ensure_bundled_sounds_installs_deactivate(tmp_path, monkeypatch) -> None:
    sounds_dir = tmp_path / "sounds"
    _patch_setup(monkeypatch, "OPENBASE_SOUNDS_DIR", sounds_dir)

    setup_cli._ensure_bundled_sounds()

    target = sounds_dir / "deactivate.wav"
    assert target.is_file()
    assert target.read_bytes().startswith(b"RIFF")


def test_ensure_bundled_sounds_preserves_custom_existing_file(
    tmp_path, monkeypatch
) -> None:
    sounds_dir = tmp_path / "sounds"
    target = sounds_dir / "deactivate.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"custom sound")
    _patch_setup(monkeypatch, "OPENBASE_SOUNDS_DIR", sounds_dir)

    setup_cli._ensure_bundled_sounds()

    assert target.read_bytes() == b"custom sound"


def test_setup_configures_tailscale_serve(tmp_path, monkeypatch) -> None:
    calls = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / ".env"

    (workspace / "cli").mkdir()
    (workspace / "multi.json").write_text("{}", encoding="utf-8")
    _patch_setup(monkeypatch, "ensure_backend_binary", lambda _backend: None)
    _patch_setup(monkeypatch, "_ensure_normal_codex_mcp", lambda _workspace_dir: None)
    _patch_setup(monkeypatch, "_ensure_normal_claude_mcp", lambda _workspace_dir: None)
    _patch_setup(monkeypatch, "_ensure_claude_auth_bridge", lambda **_kwargs: None)
    _patch_setup(
        monkeypatch,
        "_ensure_thread_sync_exchange_dir",
        lambda: calls.append("thread-sync"),
    )
    monkeypatch.setattr(
        setup_cli, "_ensure_bundled_sounds", lambda: calls.append("sounds")
    )
    _patch_setup(monkeypatch, "_ensure_env_file", lambda *_args, **_kwargs: None)
    _patch_setup(monkeypatch, "_symlink_codex_auth", lambda: None)
    _patch_setup(
        monkeypatch,
        "_ensure_normal_claude_md_symlink",
        lambda: calls.append("normal-claude"),
    )
    _patch_setup(
        monkeypatch,
        "_ensure_codex_home_default_files",
        lambda _workspace_dir: None,
    )
    monkeypatch.setattr(
        setup_cli, "_ensure_codex_home_dispatcher_config", lambda **_kwargs: None
    )
    _patch_setup(monkeypatch, "_download_local_audio_models", lambda: None)
    monkeypatch.setattr(
        setup_cli, "_symlink_codex_home_skills", lambda _workspace_dir: None
    )
    _patch_setup(monkeypatch, "_init_cli_workspace", lambda _workspace_dir: None)
    monkeypatch.setattr(
        setup_cli, "_ensure_codex_home_config", lambda *_args, **_kwargs: None
    )
    _patch_setup(
        monkeypatch, "_ensure_claude_config", lambda _workspace_dir, **_kwargs: None
    )
    _patch_setup(monkeypatch, "_install_cli_shim", lambda _workspace_dir: None)
    _patch_setup(monkeypatch, "_build_console", lambda _workspace_dir: None)
    _patch_setup(monkeypatch, "install_all_services", lambda _config: None)
    monkeypatch.setattr(
        setup_cli.InstallationConfig,
        "save",
        lambda self: None,
    )

    def fake_configure_tailscale_serve():
        calls.append("configure")

    _patch_setup(
        monkeypatch,
        "configure_tailscale_serve",
        fake_configure_tailscale_serve,
    )
    _patch_setup(
        monkeypatch,
        "tailscale_serve_health",
        lambda: type(
            "Health",
            (),
            {
                "healthy": True,
                "openbase_url": "http://mac.tailnet.ts.net:18080",
                "error": None,
            },
        )(),
    )

    runner = CliRunner()
    result = runner.invoke(
        setup_cli.setup,
        [
            "--workspace-dir",
            str(workspace),
            "--env-file",
            str(env_file),
            "--backend",
            "claude-code",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["thread-sync", "sounds", "normal-claude", "configure"]


def test_ensure_local_audio_dependencies_installs_into_runtime_python(
    tmp_path, monkeypatch
) -> None:
    python_path = tmp_path / "python"
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    runtime_package = type("RuntimePackage", (), {"python_path": python_path})()
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        if command[1:] == [
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ]:
            return subprocess.CompletedProcess(command, 0, stdout="3.12\n")
        if command[1:] == ["-c", "import huggingface_hub, kokoro, mlx_whisper"]:
            return subprocess.CompletedProcess(command, 1)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    setup_cli._ensure_local_audio_dependencies(runtime_package)

    assert [command for command, _kwargs in commands][-1] == [
        str(python_path),
        "-m",
        "pip",
        "install",
        "--upgrade",
        *setup_cli.LOCAL_AUDIO_REQUIREMENTS,
    ]


def test_ensure_local_audio_dependencies_rejects_python_313(
    tmp_path, monkeypatch
) -> None:
    python_path = tmp_path / "python"
    runtime_package = type("RuntimePackage", (), {"python_path": python_path})()

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="3.13\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(Exception, match="requires a Python 3.12"):
        setup_cli._ensure_local_audio_dependencies(runtime_package)


def test_workspace_skill_sources_supports_direct_skill_dirs(tmp_path) -> None:
    source_root = tmp_path / "skills"
    direct_skill = source_root / "direct-skill"
    nested_skill = source_root / "skills" / "nested-skill"
    direct_skill.mkdir(parents=True)
    nested_skill.mkdir(parents=True)
    (direct_skill / "SKILL.md").write_text("# Direct\n", encoding="utf-8")
    (nested_skill / "SKILL.md").write_text("# Nested\n", encoding="utf-8")

    assert setup_cli._workspace_skill_sources(source_root) == [
        nested_skill,
        direct_skill,
    ]


def test_build_console_does_not_sync_plugin_generated_files(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    console_dir = workspace / "console"
    generated_registry = console_dir / "src" / "generated" / "pluginRegistry.ts"
    console_dir.mkdir(parents=True)
    commands = []

    def fake_run_workspace_package_command(workspace_dir, package_dir, *args):
        commands.append((workspace_dir, package_dir, args))
        return True

    def fail_if_plugin_registry_is_loaded():
        raise AssertionError("setup should not sync plugin console integration")

    _patch_setup(
        monkeypatch,
        "run_workspace_package_command",
        fake_run_workspace_package_command,
    )
    monkeypatch.setattr(
        setup_cli, "load_registry", fail_if_plugin_registry_is_loaded, raising=False
    )

    setup_cli._build_console(str(workspace))

    assert commands == [
        (workspace, console_dir, ("install",)),
        (workspace, console_dir, ("run", "build")),
    ]
    assert not generated_registry.exists()


def test_super_agents_mcp_command_routes_through_current_symlink(
    tmp_path, monkeypatch
) -> None:
    from openbase_coder_cli import runtime as runtime_module

    workspace = tmp_path / "workspace"
    release = tmp_path / "packages" / "releases" / "1.0.0"
    python_path = release / "python" / "bin" / "python"
    command = python_path.parent / "super-agents-mcp"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    current = tmp_path / "packages" / "current"
    current.symlink_to(release)
    monkeypatch.setattr(runtime_module, "STANDALONE_CURRENT_DIR", current)
    _patch_setup(
        monkeypatch,
        "current_runtime_package",
        lambda: SimpleNamespace(python_path=python_path),
    )
    _patch_setup(monkeypatch, "which", lambda _command: None)

    command_path, args = setup_cli._super_agents_mcp_command(workspace)

    assert command_path == current / "python" / "bin" / "super-agents-mcp"
    assert args == []
    assert command_path.is_file()


def test_symlink_codex_home_skills_repoints_version_pinned_links(
    tmp_path, monkeypatch
) -> None:
    """A link pinned to the versioned release resolves identically to the
    stable current/ alias today, but must still be migrated."""
    from openbase_coder_cli import runtime as runtime_module

    release = tmp_path / "packages" / "releases" / "1.0.0"
    skill = release / "skills" / "sample-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Sample\n", encoding="utf-8")
    current = tmp_path / "packages" / "current"
    current.symlink_to(release)
    monkeypatch.setattr(runtime_module, "STANDALONE_CURRENT_DIR", current)
    codex_home, claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    target = codex_home / "skills" / "sample-skill"
    target.parent.mkdir(parents=True)
    target.symlink_to(skill)
    _patch_setup(monkeypatch, "packaged_skills_dir", lambda: release / "skills")

    setup_cli._symlink_codex_home_skills("")

    assert target.readlink() == current / "skills" / "sample-skill"
    claude_target = claude_config / "skills" / "sample-skill"
    assert claude_target.readlink() == current / "skills" / "sample-skill"


def test_relink_workspace_skills_heals_foreign_home_symlinks(
    tmp_path, monkeypatch
) -> None:
    """Cross-machine file sync can replace skill links with another machine's
    home paths; the startup relink must re-point them at this checkout."""
    workspace = tmp_path / "workspace"
    skill = workspace / "skills" / "skills" / "sample-skill"
    codex_home, claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Sample\n", encoding="utf-8")
    for target_root in (codex_home / "skills", claude_config / "skills"):
        target_root.mkdir(parents=True)
        (target_root / "sample-skill").symlink_to(
            "/home/ubuntu/.openbase/openbase-coder-workspace/skills/skills/sample-skill"
        )

    from openbase_coder_cli.cli.setup import codex as codex_setup
    from openbase_coder_cli.services import installation

    monkeypatch.setattr(
        installation.InstallationConfig, "exists", classmethod(lambda cls: True)
    )
    monkeypatch.setattr(
        installation.InstallationConfig,
        "load",
        classmethod(
            lambda cls: installation.InstallationConfig(workspace_path=str(workspace))
        ),
    )

    assert codex_setup.relink_workspace_skills_from_installation() is True
    for target_root in (codex_home / "skills", claude_config / "skills"):
        target = target_root / "sample-skill"
        assert target.is_symlink()
        assert target.resolve() == skill.resolve()


def test_relink_workspace_skills_noop_without_installation(monkeypatch) -> None:
    from openbase_coder_cli.cli.setup import codex as codex_setup
    from openbase_coder_cli.services import installation

    monkeypatch.setattr(
        installation.InstallationConfig, "exists", classmethod(lambda cls: False)
    )

    assert codex_setup.relink_workspace_skills_from_installation() is False


def _fake_tty_stdin(monkeypatch, text: str) -> None:
    import io
    import sys

    class _FakeTTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", _FakeTTY(text))


def test_require_backend_choice_picker_maps_numbers(tmp_path, monkeypatch) -> None:
    _fake_tty_stdin(monkeypatch, "3\n")

    choice = setup_cli._require_backend_choice(
        str(tmp_path / ".env"), None, interactive=True
    )

    assert choice == "openbase_cloud"


def test_require_backend_choice_keeps_existing_env(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=codex\n")
    _fake_tty_stdin(monkeypatch, "1\n")

    assert (
        setup_cli._require_backend_choice(str(env_file), None, interactive=True) is None
    )


def test_require_audio_provider_choice_picker_maps_numbers(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        setup_cli,
        "CODEX_DISPATCHER_CONFIG_PATH",
        tmp_path / "dispatcher-config.json",
    )
    _fake_tty_stdin(monkeypatch, "2\n")

    assert setup_cli._require_audio_provider_choice(None, interactive=True) == (
        setup_cli.AUDIO_PROVIDER_CARTESIA
    )


def test_require_audio_provider_choice_enter_uses_cloud_default(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        setup_cli,
        "CODEX_DISPATCHER_CONFIG_PATH",
        tmp_path / "dispatcher-config.json",
    )
    _fake_tty_stdin(monkeypatch, "\n")

    assert setup_cli._require_audio_provider_choice(None, interactive=True) == (
        setup_cli.AUDIO_PROVIDER_OPENBASE_CLOUD
    )


def test_require_audio_provider_choice_keeps_existing_dispatcher_config(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text("{}\n")
    monkeypatch.setattr(setup_cli, "CODEX_DISPATCHER_CONFIG_PATH", config_path)
    _fake_tty_stdin(monkeypatch, "3\n")

    assert setup_cli._require_audio_provider_choice(None, interactive=True) is None


def test_require_audio_provider_choice_non_interactive_keeps_default(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        setup_cli,
        "CODEX_DISPATCHER_CONFIG_PATH",
        tmp_path / "dispatcher-config.json",
    )

    assert setup_cli._require_audio_provider_choice(None, interactive=False) is None


def test_require_byok_audio_keys_prompts_for_missing_keys(
    tmp_path, monkeypatch
) -> None:
    _fake_tty_stdin(monkeypatch, "aai-key\ncartesia-key\n")

    keys = setup_cli._require_byok_audio_keys(
        str(tmp_path / ".env"),
        setup_cli.AUDIO_PROVIDER_CARTESIA,
        "",
        "",
        interactive=True,
    )

    assert keys == ("aai-key", "cartesia-key")


def test_require_byok_audio_keys_leaves_existing_env_alone(
    tmp_path, monkeypatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("")
    _fake_tty_stdin(monkeypatch, "unused\n")

    keys = setup_cli._require_byok_audio_keys(
        str(env_file),
        setup_cli.AUDIO_PROVIDER_CARTESIA,
        "",
        "",
        interactive=True,
    )

    assert keys == ("", "")


def test_require_byok_audio_keys_skips_other_providers(tmp_path, monkeypatch) -> None:
    _fake_tty_stdin(monkeypatch, "unused\n")

    keys = setup_cli._require_byok_audio_keys(
        str(tmp_path / ".env"),
        setup_cli.AUDIO_PROVIDER_OPENBASE_CLOUD,
        "",
        "",
        interactive=True,
    )

    assert keys == ("", "")


def test_resolve_interactive_mode_no_flags_on_tty(monkeypatch) -> None:
    _fake_tty_stdin(monkeypatch, "")
    ctx = setup_cli.setup.make_context("setup", [])
    with ctx:
        assert setup_cli._resolve_interactive_mode(None, False) is True


def test_resolve_interactive_mode_any_flag_disables_prompts(monkeypatch) -> None:
    _fake_tty_stdin(monkeypatch, "")
    ctx = setup_cli.setup.make_context("setup", ["--skip-services"])
    with ctx:
        assert setup_cli._resolve_interactive_mode(None, False) is False


def test_resolve_interactive_mode_workspace_dir_flag_disables_prompts(
    tmp_path, monkeypatch
) -> None:
    _fake_tty_stdin(monkeypatch, "")
    ctx = setup_cli.setup.make_context(
        "setup", ["--workspace-dir", str(tmp_path), "--backend", "codex"]
    )
    with ctx:
        assert setup_cli._resolve_interactive_mode(None, False) is False


def test_resolve_interactive_mode_forced_interactive_wins(monkeypatch) -> None:
    _fake_tty_stdin(monkeypatch, "")
    ctx = setup_cli.setup.make_context("setup", ["--interactive", "--skip-services"])
    with ctx:
        assert setup_cli._resolve_interactive_mode(True, False) is True


def test_resolve_interactive_mode_forced_non_interactive_wins(monkeypatch) -> None:
    _fake_tty_stdin(monkeypatch, "")
    ctx = setup_cli.setup.make_context("setup", ["--non-interactive"])
    with ctx:
        assert setup_cli._resolve_interactive_mode(False, False) is False


def test_resolve_interactive_mode_json_progress_disables_prompts(
    monkeypatch,
) -> None:
    _fake_tty_stdin(monkeypatch, "")
    ctx = setup_cli.setup.make_context("setup", ["--interactive", "--json-progress"])
    with ctx:
        assert setup_cli._resolve_interactive_mode(True, True) is False


def test_resolve_interactive_mode_non_tty_disables_prompts(monkeypatch) -> None:
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    ctx = setup_cli.setup.make_context("setup", [])
    with ctx:
        assert setup_cli._resolve_interactive_mode(None, False) is False


def test_interactive_login_checks_skip_when_declined(tmp_path, monkeypatch) -> None:
    class _LoggedOut:
        def __init__(self, url):
            self.has_refresh_token = False

    calls = []
    monkeypatch.setattr(setup_cli, "TokenManager", _LoggedOut)
    monkeypatch.setattr(setup_cli, "register_and_report", lambda **kw: calls.append(kw))
    _fake_tty_stdin(monkeypatch, "n\n")

    setup_cli._interactive_cloud_login_and_checks(
        str(tmp_path / ".env"), cli_configured=True
    )

    assert calls == []


def test_interactive_login_checks_report_when_logged_in(tmp_path, monkeypatch) -> None:
    class _LoggedIn:
        def __init__(self, url):
            self.has_refresh_token = True

    reports = []
    monkeypatch.setattr(setup_cli, "TokenManager", _LoggedIn)
    monkeypatch.setattr(
        setup_cli,
        "tailscale_serve_health",
        lambda: SimpleNamespace(healthy=True, error=None),
    )

    def fake_report(**kwargs):
        reports.append(kwargs)
        return SimpleNamespace(ok=True, supported=True, error=None)

    monkeypatch.setattr(setup_cli, "register_and_report", fake_report)

    setup_cli._interactive_cloud_login_and_checks(
        str(tmp_path / ".env"), cli_configured=True
    )

    assert reports == [{"cli_configured": True, "serve_healthy": True}]


def test_print_app_download_qr_outputs_url(capsys) -> None:
    setup_cli._print_app_download_qr()

    out = capsys.readouterr().out
    assert "https://openbase.cloud/downloads.html" in out
    assert "█" in out or "▀" in out or "▄" in out
