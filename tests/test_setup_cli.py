from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from openbase_coder_cli.cli.setup.hooks import session_start_hook_trusted_hash
from openbase_coder_cli.paths import (
    INJECT_SESSION_ID_HOOK_PATH,
    OPENBASE_AGENTS_MD_PATH,
)

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
    """Point the shared agent homes (~/.codex, ~/.claude, ~/.claude.json) and
    the Openbase instructions dir (~/.openbase/instructions) into tmp_path."""
    codex_home = tmp_path / "codex_home"
    claude_config = tmp_path / "claude_config"
    instructions = tmp_path / "openbase" / "instructions"
    _patch_setup(monkeypatch, "CODEX_HOME_DIR", codex_home)
    _patch_setup(monkeypatch, "CODEX_CONFIG_PATH", codex_home / "config.toml")
    _patch_setup(monkeypatch, "CODEX_AGENTS_MD_PATH", codex_home / "AGENTS.md")
    _patch_setup(monkeypatch, "CLAUDE_CONFIG_DIR", claude_config)
    _patch_setup(monkeypatch, "CLAUDE_SETTINGS_PATH", claude_config / "settings.json")
    _patch_setup(monkeypatch, "CLAUDE_STATE_PATH", tmp_path / ".claude.json")
    _patch_setup(monkeypatch, "OPENBASE_AGENTS_MD_PATH", instructions / "AGENTS.md")
    monkeypatch.setattr(
        codex_home_instructions,
        "OPENBASE_AGENTS_MD_PATH",
        instructions / "AGENTS.md",
    )
    monkeypatch.setattr(codex_home_instructions, "is_standalone_runtime", lambda: False)
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
    _patch_setup(
        monkeypatch,
        "OPENBASE_INSTRUCTION_FILES",
        (
            ("VOICE_INSTRUCTIONS.md", instructions / "VOICE_INSTRUCTIONS.md"),
            ("DISPATCHER_INSTRUCTIONS.md", instructions / "DISPATCHER_INSTRUCTIONS.md"),
            (
                "SUPER_AGENT_INSTRUCTIONS.md",
                instructions / "SUPER_AGENT_INSTRUCTIONS.md",
            ),
        ),
    )
    return codex_home, claude_config


def _make_workspace_checkout(root):
    (root / "cli").mkdir(parents=True)
    (root / "multi.json").write_text("{}", encoding="utf-8")
    return root


def test_setup_windows_proceeds_past_os_guard(monkeypatch) -> None:
    monkeypatch.setattr(
        "openbase_coder_cli.platforms.current_system", lambda: "Windows"
    )
    sentinel = RuntimeError("reached backend resolution")

    def _raise(*_args, **_kwargs):
        raise sentinel

    monkeypatch.setattr(setup_cli, "_require_backend_choice", _raise)

    runner = CliRunner()
    result = runner.invoke(setup_cli.setup, ["--backend", "claude-code"])

    assert "Setup is only supported" not in (result.output or "")
    assert result.exception is sentinel


def test_setup_rejects_unsupported_os(monkeypatch) -> None:
    monkeypatch.setattr("openbase_coder_cli.platforms.current_system", lambda: "SunOS")

    runner = CliRunner()
    result = runner.invoke(setup_cli.setup, ["--backend", "claude-code"])

    assert result.exit_code != 0
    assert "Setup is only supported" in result.output


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


def test_ensure_openbase_instruction_files_renders_role_files(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    instructions = workspace / "instructions"
    instructions.mkdir(parents=True)
    codex_home, claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    shared_instructions = tmp_path / "openbase" / "instructions"
    names = (
        "VOICE_INSTRUCTIONS.md",
        "DISPATCHER_INSTRUCTIONS.md",
        "SUPER_AGENT_INSTRUCTIONS.md",
    )
    (instructions / "AGENTS.md").write_text("- Openbase rule\n", encoding="utf-8")
    for resource_name in names:
        (instructions / resource_name).write_text(
            f"default {resource_name}\n", encoding="utf-8"
        )

    setup_cli._ensure_openbase_instruction_files(str(workspace))

    for resource_name in names:
        target_path = shared_instructions / resource_name
        assert target_path.is_file()
        assert not target_path.is_symlink()
        assert target_path.read_text(encoding="utf-8") == (
            f"<!-- Generated from {instructions / resource_name}; "
            "edit the source template instead. -->\n\n"
            f"default {resource_name}\n"
        )
    agents_path = shared_instructions / "AGENTS.md"
    assert agents_path.read_text(encoding="utf-8") == (
        "## Openbase Coder Instructions\n\n"
        f"- These instructions are auto generated from {instructions / 'AGENTS.md'}."
        "\n\n"
        "- Openbase rule\n"
    )
    # The shared agent homes are never generated into.
    assert not codex_home.exists()
    assert not claude_config.exists()


def test_ensure_openbase_instruction_files_renders_template_variables(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    instructions = workspace / "instructions"
    instructions.mkdir(parents=True)
    _patch_openbase_agent_paths(monkeypatch, tmp_path)
    target = tmp_path / "openbase" / "instructions" / "SUPER_AGENT_INSTRUCTIONS.md"
    (instructions / "SUPER_AGENT_INSTRUCTIONS.md").write_text(
        'Require "${dangerous_confirmation_phrase}".\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "openbase_coder_cli.instruction_templates.get_dangerous_confirmation_phrase",
        lambda: "ship it",
    )

    setup_cli._ensure_openbase_instruction_files(str(workspace))

    assert target.is_file()
    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8") == (
        f"<!-- Generated from {instructions / 'SUPER_AGENT_INSTRUCTIONS.md'}; "
        'edit the source template instead. -->\n\nRequire "ship it".\n'
    )


def test_ensure_openbase_instruction_files_preserves_custom_role_file(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    instructions = workspace / "instructions"
    instructions.mkdir(parents=True)
    _patch_openbase_agent_paths(monkeypatch, tmp_path)
    target = tmp_path / "openbase" / "instructions" / "VOICE_INSTRUCTIONS.md"
    target.parent.mkdir(parents=True)
    target.write_text("custom voice instructions\n", encoding="utf-8")
    (instructions / "VOICE_INSTRUCTIONS.md").write_text(
        "default voice\n", encoding="utf-8"
    )

    setup_cli._ensure_openbase_instruction_files(str(workspace))

    assert target.read_text(encoding="utf-8") == "custom voice instructions\n"


def test_ensure_openbase_instruction_files_rewrites_generated_role_file(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    instructions = workspace / "instructions"
    instructions.mkdir(parents=True)
    _patch_openbase_agent_paths(monkeypatch, tmp_path)
    target = tmp_path / "openbase" / "instructions" / "VOICE_INSTRUCTIONS.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "<!-- Generated from /old/source/VOICE_INSTRUCTIONS.md; "
        "edit the source template instead. -->\n\nstale voice\n",
        encoding="utf-8",
    )
    (instructions / "VOICE_INSTRUCTIONS.md").write_text(
        "default voice\n", encoding="utf-8"
    )

    setup_cli._ensure_openbase_instruction_files(str(workspace))

    assert target.read_text(encoding="utf-8") == (
        f"<!-- Generated from {instructions / 'VOICE_INSTRUCTIONS.md'}; "
        "edit the source template instead. -->\n\n"
        "default voice\n"
    )


def test_ensure_openbase_instruction_files_replaces_stale_agents_symlink(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    instructions = workspace / "instructions"
    stale_dir = tmp_path / "stale-instructions"
    instructions.mkdir(parents=True)
    stale_dir.mkdir()
    _patch_openbase_agent_paths(monkeypatch, tmp_path)
    stale_agents = stale_dir / "AGENTS.md"
    stale_agents.write_text("- Stale rule\n", encoding="utf-8")
    (instructions / "AGENTS.md").write_text("- Openbase rule\n", encoding="utf-8")
    agents_path = tmp_path / "openbase" / "instructions" / "AGENTS.md"
    agents_path.parent.mkdir(parents=True)
    agents_path.symlink_to(stale_agents)

    setup_cli._ensure_openbase_instruction_files(str(workspace))

    assert not agents_path.is_symlink()
    assert agents_path.read_text(encoding="utf-8") == (
        "## Openbase Coder Instructions\n\n"
        f"- These instructions are auto generated from {instructions / 'AGENTS.md'}."
        "\n\n"
        "- Openbase rule\n"
    )
    assert stale_agents.read_text(encoding="utf-8") == "- Stale rule\n"


def test_ensure_openbase_instruction_files_skips_missing_sources(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _patch_openbase_agent_paths(monkeypatch, tmp_path)
    shared_instructions = tmp_path / "openbase" / "instructions"

    setup_cli._ensure_openbase_instruction_files(str(workspace))

    assert not shared_instructions.exists()


def test_ensure_claude_md_symlink_links_claude_md_to_codex_agents(
    tmp_path, monkeypatch
) -> None:
    codex_home, claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    agents_path = codex_home / "AGENTS.md"
    agents_path.parent.mkdir(parents=True)
    agents_path.write_text("shared instructions\n", encoding="utf-8")

    setup_cli._ensure_claude_md_symlink()

    claude_md_path = claude_config / "CLAUDE.md"
    assert claude_md_path.is_symlink()
    assert claude_md_path.readlink() == Path(
        os.path.relpath(agents_path, claude_config)
    )
    assert claude_md_path.resolve() == agents_path.resolve()


def test_ensure_claude_md_symlink_migrates_existing_claude_file(
    tmp_path, monkeypatch
) -> None:
    codex_home, claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    claude_md_path = claude_config / "CLAUDE.md"
    claude_md_path.parent.mkdir(parents=True)
    claude_md_path.write_text("user claude instructions\n", encoding="utf-8")

    setup_cli._ensure_claude_md_symlink()

    agents_path = codex_home / "AGENTS.md"
    assert agents_path.read_text(encoding="utf-8") == "user claude instructions\n"
    assert claude_md_path.is_symlink()
    assert claude_md_path.resolve() == agents_path.resolve()


def test_ensure_claude_md_symlink_backs_up_different_file(
    tmp_path, monkeypatch
) -> None:
    codex_home, claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    agents_path = codex_home / "AGENTS.md"
    claude_md_path = claude_config / "CLAUDE.md"
    agents_path.parent.mkdir(parents=True)
    claude_md_path.parent.mkdir(parents=True)
    agents_path.write_text("codex agents\n", encoding="utf-8")
    claude_md_path.write_text("different claude\n", encoding="utf-8")

    setup_cli._ensure_claude_md_symlink()

    backups = list(claude_md_path.parent.glob("CLAUDE.md.backup-openbase-coder-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "different claude\n"
    assert claude_md_path.is_symlink()
    assert claude_md_path.resolve() == agents_path.resolve()
    assert agents_path.read_text(encoding="utf-8") == "codex agents\n"


def test_ensure_claude_md_symlink_repoints_stale_symlink(tmp_path, monkeypatch) -> None:
    codex_home, claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    stale_target = tmp_path / "stale" / "AGENTS.md"
    stale_target.parent.mkdir(parents=True)
    stale_target.write_text("stale\n", encoding="utf-8")
    agents_path = codex_home / "AGENTS.md"
    agents_path.parent.mkdir(parents=True)
    agents_path.write_text("codex agents\n", encoding="utf-8")
    claude_md_path = claude_config / "CLAUDE.md"
    claude_md_path.parent.mkdir(parents=True)
    claude_md_path.symlink_to(stale_target)

    setup_cli._ensure_claude_md_symlink()

    assert claude_md_path.is_symlink()
    assert claude_md_path.readlink() == Path(
        os.path.relpath(agents_path, claude_config)
    )
    assert claude_md_path.resolve() == agents_path.resolve()


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


def _expected_session_id_hook_suffix(config_path: Path) -> str:
    hook_command = str(INJECT_SESSION_ID_HOOK_PATH)
    resolved_path = config_path.parent.resolve() / config_path.name
    state_key = f"{resolved_path}:session_start:0:0"
    return (
        "\n"
        "[[hooks.SessionStart]]\n"
        "\n"
        "[[hooks.SessionStart.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(hook_command)}\n"
        "\n"
        f"[hooks.state.{json.dumps(state_key)}]\n"
        f"trusted_hash = {json.dumps(session_start_hook_trusted_hash(hook_command))}\n"
        "enabled = true\n"
    )


# Openbase passes its permission posture per session through the MCP env, so
# it never has to write sandbox/approval values into the user's config.
SUPER_AGENTS_PERMISSION_ENV_SUFFIX = (
    'SUPER_AGENTS_CODEX_APPROVAL_POLICY = "never", '
    'SUPER_AGENTS_CODEX_SANDBOX_POLICY = "danger-full-access"'
)


def test_ensure_codex_config_registers_super_agents_mcp(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    command = workspace / ".venv" / "bin" / "super-agents-mcp"
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n", encoding="utf-8")

    setup_cli._ensure_codex_config(str(workspace))

    config_path = codex_home / "config.toml"
    assert config_path.read_text(encoding="utf-8") == (
        "[mcp_servers.super-agents]\n"
        f"command = {json.dumps(str(command))}\n"
        'env = { SUPER_AGENTS_DEFAULT_BACKEND = "codex", '
        + SUPER_AGENTS_PERMISSION_ENV_SUFFIX
        + " }\n"
        + _expected_session_id_hook_suffix(config_path)
    )


def test_ensure_codex_config_preserves_user_config_values(
    tmp_path, monkeypatch
) -> None:
    """The shared ~/.codex/config.toml keeps the user's own settings; only the
    super-agents table is managed and no root sandbox/approval values are
    ever written."""
    workspace = tmp_path / "workspace"
    command = workspace / ".venv" / "bin" / "super-agents-mcp"
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    config_path = codex_home / "config.toml"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                'sandbox_mode = "workspace-write"',
                'approval_policy = "on-request"',
                'model = "gpt-5.5-mini"',
                "",
                '[projects."/Users/example"]',
                'trust_level = "trusted"',
                "",
                "[mcp_servers.super-agents]",
                'command = "/Users/example/.local/bin/uv"',
                'args = ["--directory", "/bad", "run", "super-agents-mcp"]',
                "",
                "[mcp_servers.playwright]",
                'command = "npx"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    setup_cli._ensure_codex_config(str(workspace))

    updated = config_path.read_text(encoding="utf-8")
    assert 'sandbox_mode = "workspace-write"' in updated
    assert 'approval_policy = "on-request"' in updated
    assert 'model = "gpt-5.5-mini"' in updated
    assert 'sandbox_mode = "danger-full-access"' not in updated
    assert updated.count("[mcp_servers.super-agents]") == 1
    assert "/Users/example/.local/bin/uv" not in updated
    assert "args =" not in updated
    assert '[projects."/Users/example"]\ntrust_level = "trusted"' in updated
    assert f"command = {json.dumps(str(command))}" in updated
    assert '[mcp_servers.playwright]\ncommand = "npx"' in updated
    assert SUPER_AGENTS_PERMISSION_ENV_SUFFIX in updated


def test_ensure_codex_config_preserves_cloud_codex_child_default(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    command = workspace / ".venv" / "bin" / "super-agents-mcp"
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n", encoding="utf-8")

    setup_cli._ensure_codex_config(
        str(workspace),
        coding_backend="openbase_cloud_codex",
    )

    assert (
        'env = { SUPER_AGENTS_DEFAULT_BACKEND = "openbase_cloud_codex", '
        + SUPER_AGENTS_PERMISSION_ENV_SUFFIX
        + " }"
    ) in (codex_home / "config.toml").read_text(encoding="utf-8")


def test_ensure_codex_config_falls_back_to_resolved_uv(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    cli_dir = workspace / "cli"
    uv_bin = tmp_path / "homebrew" / "bin" / "uv"
    codex_home, _claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    cli_dir.mkdir(parents=True)
    uv_bin.parent.mkdir(parents=True)
    uv_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    _patch_setup(monkeypatch, "current_runtime_package", lambda: None)
    _patch_setup(
        monkeypatch,
        "which",
        lambda command: str(uv_bin) if command == "uv" else None,
    )

    setup_cli._ensure_codex_config(str(workspace))

    config_path = codex_home / "config.toml"
    assert config_path.read_text(encoding="utf-8") == (
        "[mcp_servers.super-agents]\n"
        f"command = {json.dumps(str(uv_bin))}\n"
        f"args = {json.dumps(['--directory', str(cli_dir), 'run', 'super-agents-mcp'])}\n"
        'env = { SUPER_AGENTS_DEFAULT_BACKEND = "codex", '
        + SUPER_AGENTS_PERMISSION_ENV_SUFFIX
        + " }\n"
        + _expected_session_id_hook_suffix(config_path)
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


def test_ensure_claude_mcp_installs_super_agents(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    command = workspace / ".venv" / "bin" / "super-agents-mcp"
    dispatcher_config = tmp_path / "dispatcher-config.json"
    _codex_home, claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    instructions = tmp_path / "openbase" / "instructions"
    state_path = tmp_path / ".claude.json"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "firstStartTime": "2026-06-18T00:00:00.000Z",
                "mcpServers": {"playwright": {"command": "npx"}},
            }
        ),
        encoding="utf-8",
    )
    _patch_setup(monkeypatch, "CODEX_DISPATCHER_CONFIG_PATH", dispatcher_config)

    setup_cli._ensure_claude_mcp(str(workspace))

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["firstStartTime"] == "2026-06-18T00:00:00.000Z"
    assert payload["mcpServers"]["playwright"] == {"command": "npx"}
    assert payload["mcpServers"]["super-agents"] == {
        "type": "stdio",
        "command": str(command),
        "env": {
            "SUPER_AGENTS_DEFAULT_CONFIG_PATH": str(dispatcher_config),
            "CODEX_SUPER_AGENT_INSTRUCTIONS_PATH": str(
                instructions / "SUPER_AGENT_INSTRUCTIONS.md"
            ),
            "SUPER_AGENTS_BASE_INSTRUCTIONS_PATH": str(instructions / "AGENTS.md"),
            "SUPER_AGENTS_DEFAULT_BACKEND": "claude_code",
        },
    }
    # The user's Claude settings are never touched by MCP registration.
    assert not (claude_config / "settings.json").exists()


def test_ensure_claude_mcp_preserves_cloud_child_default(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    command = workspace / ".venv" / "bin" / "super-agents-mcp"
    _patch_openbase_agent_paths(monkeypatch, tmp_path)
    state_path = tmp_path / ".claude.json"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n", encoding="utf-8")

    setup_cli._ensure_claude_mcp(
        str(workspace),
        coding_backend="openbase_cloud",
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert (
        payload["mcpServers"]["super-agents"]["env"]["SUPER_AGENTS_DEFAULT_BACKEND"]
        == "openbase_cloud"
    )


def test_ensure_claude_hooks_registers_session_id_hook(tmp_path, monkeypatch) -> None:
    _codex_home, claude_config = _patch_openbase_agent_paths(monkeypatch, tmp_path)
    settings_path = claude_config / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"model": "sonnet", "permissions": {"defaultMode": "auto"}}),
        encoding="utf-8",
    )

    setup_cli._ensure_claude_hooks()

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["model"] == "sonnet"
    assert settings["permissions"] == {"defaultMode": "auto"}
    assert settings["hooks"]["SessionStart"] == [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": str(INJECT_SESSION_ID_HOOK_PATH)}],
        }
    ]


def test_selected_coding_backend_reads_existing_env(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=claude_code\n", encoding="utf-8")

    assert setup_cli._selected_coding_backend(env_file, None) == "claude_code"


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
    assert "CLAUDE_CONFIG_DIR" not in content
    assert "SUPER_AGENTS_DEFAULT_CONFIG_PATH=" in content
    assert "SUPER_AGENTS_CODEX_APPROVAL_POLICY=never" in content
    assert "SUPER_AGENTS_CODEX_SANDBOX_POLICY=danger-full-access" in content
    assert f"SUPER_AGENTS_BASE_INSTRUCTIONS_PATH={OPENBASE_AGENTS_MD_PATH}" in content
    assert "CLAUDE_CODE_ENABLE_TELEMETRY=0" in content
    assert "CODEX_MODEL=" not in content
    assert "CODEX_APP_SERVER_URL=unix://" in content
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_ensure_env_file_migrates_existing_env_to_shared_homes(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KEEP_ME=1\n"
        "CLAUDE_CONFIG_DIR=/Users/dev/.openbase/claude_config\n"
        "LIVEKIT_API_KEY=APIkeyServer\n"
        "LIVEKIT_API_SECRET=server-secret\n"
        "LIVEKIT_CLIENT_API_KEY=APIkeyClient\n"
        "LIVEKIT_CLIENT_API_SECRET=client-secret\n",
        encoding="utf-8",
    )
    env_file.chmod(0o644)

    setup_cli._ensure_env_file(
        str(env_file),
        assembly_ai_api_key="",
        cartesia_api_key="",
    )

    content = env_file.read_text(encoding="utf-8")
    assert "KEEP_ME=1" in content
    assert "CLAUDE_CONFIG_DIR" not in content
    assert "SUPER_AGENTS_CODEX_APPROVAL_POLICY=never" in content
    assert "SUPER_AGENTS_CODEX_SANDBOX_POLICY=danger-full-access" in content
    assert f"SUPER_AGENTS_BASE_INSTRUCTIONS_PATH={OPENBASE_AGENTS_MD_PATH}" in content
    assert "SUPER_AGENTS_DEFAULT_CONFIG_PATH=" in content
    assert "CODEX_APP_SERVER_URL=unix://" in content
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_ensure_env_file_migrates_legacy_app_server_but_preserves_custom_endpoint(
    tmp_path,
) -> None:
    legacy = tmp_path / "legacy.env"
    legacy.write_text("CODEX_APP_SERVER_URL=ws://127.0.0.1:4500\n", encoding="utf-8")
    custom = tmp_path / "custom.env"
    custom.write_text(
        "CODEX_APP_SERVER_URL=wss://codex.example/rpc\n", encoding="utf-8"
    )

    for env_file in (legacy, custom):
        setup_cli._ensure_env_file(
            str(env_file),
            assembly_ai_api_key="",
            cartesia_api_key="",
        )

    assert "CODEX_APP_SERVER_URL=unix://" in legacy.read_text(encoding="utf-8")
    assert "CODEX_APP_SERVER_URL=wss://codex.example/rpc" in custom.read_text(
        encoding="utf-8"
    )


def test_ensure_env_file_keeps_user_claude_config_dir_override(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CLAUDE_CONFIG_DIR=/Users/dev/.claude-alt\n"
        "LIVEKIT_API_KEY=APIkeyServer\n"
        "LIVEKIT_API_SECRET=server-secret\n"
        "LIVEKIT_CLIENT_API_KEY=APIkeyClient\n"
        "LIVEKIT_CLIENT_API_SECRET=client-secret\n",
        encoding="utf-8",
    )

    setup_cli._ensure_env_file(
        str(env_file),
        assembly_ai_api_key="",
        cartesia_api_key="",
    )

    content = env_file.read_text(encoding="utf-8")
    assert "CLAUDE_CONFIG_DIR=/Users/dev/.claude-alt" in content


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


def test_ensure_env_file_selects_embedded_livekit_mode_for_fresh_setup(
    tmp_path,
) -> None:
    env_file = tmp_path / ".env"

    setup_cli._ensure_env_file(
        str(env_file),
        assembly_ai_api_key="",
        cartesia_api_key="",
        tailnet_provider="netmesh-tsnet",
    )

    values = setup_cli._env_file_values(env_file)
    assert values["OPENBASE_CODER_CLI_TAILSCALE_PROVIDER"] == "netmesh-tsnet"
    assert values["LIVEKIT_NETWORK_MODE"] == "local"


def test_ensure_env_file_updates_livekit_mode_with_existing_provider(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENBASE_CODER_CLI_TAILSCALE_PROVIDER=tailscale\n"
        "LIVEKIT_NETWORK_MODE=tailscale\n"
        "LIVEKIT_NODE_IP=100.64.0.9\n",
        encoding="utf-8",
    )

    setup_cli._ensure_env_file(
        str(env_file),
        assembly_ai_api_key="",
        cartesia_api_key="",
        tailnet_provider="netmesh-tsnet",
    )

    values = setup_cli._env_file_values(env_file)
    assert values["OPENBASE_CODER_CLI_TAILSCALE_PROVIDER"] == "netmesh-tsnet"
    assert values["LIVEKIT_NETWORK_MODE"] == "local"
    assert values["LIVEKIT_NODE_IP"] == ""


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


def test_setup_configures_routes_and_defers_netmesh_until_login(
    tmp_path, monkeypatch
) -> None:
    calls = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / ".env"

    (workspace / "cli").mkdir()
    (workspace / "multi.json").write_text("{}", encoding="utf-8")
    _patch_setup(monkeypatch, "OPENBASE_BASE_DIR", tmp_path / "openbase")
    _patch_setup(monkeypatch, "current_runtime_package", lambda: None)
    _patch_setup(monkeypatch, "ensure_backend_binary", lambda _backend: None)
    _patch_setup(monkeypatch, "ensure_pinned_livekit_server", lambda: None)
    _patch_setup(
        monkeypatch,
        "claude_auth_status",
        lambda: SimpleNamespace(logged_in=False, raw_output="{}", returncode=0),
    )
    _patch_setup(
        monkeypatch,
        "_ensure_thread_sync_exchange_dir",
        lambda: calls.append("thread-sync"),
    )
    monkeypatch.setattr(
        setup_cli, "_ensure_bundled_sounds", lambda: calls.append("sounds")
    )
    _patch_setup(monkeypatch, "_ensure_env_file", lambda *_args, **_kwargs: None)
    _patch_setup(
        monkeypatch,
        "_ensure_claude_md_symlink",
        lambda: calls.append("claude-md"),
    )
    _patch_setup(
        monkeypatch,
        "_ensure_openbase_instruction_files",
        lambda _workspace_dir: None,
    )
    monkeypatch.setattr(
        setup_cli, "_ensure_codex_home_dispatcher_config", lambda **_kwargs: None
    )
    _patch_setup(monkeypatch, "set_dispatcher_service_tier", lambda _tier: None)
    _patch_setup(monkeypatch, "_download_local_audio_models", lambda: None)
    monkeypatch.setattr(
        setup_cli, "_symlink_codex_home_skills", lambda _workspace_dir: None
    )
    _patch_setup(
        monkeypatch,
        "_init_cli_workspace",
        lambda _workspace_dir, **_kwargs: None,
    )
    _patch_setup(monkeypatch, "_ensure_session_id_hook_script", lambda: None)
    monkeypatch.setattr(
        setup_cli, "_ensure_codex_config", lambda *_args, **_kwargs: None
    )
    _patch_setup(
        monkeypatch, "_ensure_claude_mcp", lambda _workspace_dir, **_kwargs: None
    )
    _patch_setup(monkeypatch, "_ensure_claude_hooks", lambda: None)
    _patch_setup(monkeypatch, "_install_cli_shim", lambda _workspace_dir: None)
    _patch_setup(monkeypatch, "_build_console", lambda _workspace_dir: None)
    _patch_setup(monkeypatch, "install_all_services", lambda _config: None)
    _patch_setup(
        monkeypatch,
        "install_tunneld_binary",
        lambda _config: calls.append("tunneld-binary") or tmp_path / "openbase-tunneld",
    )
    _patch_setup(
        monkeypatch,
        "install_service",
        lambda _config, service: calls.append(f"service:{service.name}"),
    )
    _patch_setup(monkeypatch, "compute_cli_configured", lambda: True)
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
    assert calls == ["thread-sync", "sounds", "claude-md", "configure"]
    assert "Claude Code is not logged in" in result.output

    tailnet_cli = importlib.import_module("openbase_coder_cli.cli.tailnet")

    calls.clear()
    monkeypatch.setattr(
        tailnet_cli,
        "_provision_netmesh_companion",
        lambda: calls.append("provision-netmesh"),
    )

    def unavailable_before_login():
        calls.append("configure")
        raise RuntimeError("netmesh control socket is not ready")

    _patch_setup(
        monkeypatch,
        "configure_tailscale_serve",
        unavailable_before_login,
    )
    result = runner.invoke(
        setup_cli.setup,
        [
            "--workspace-dir",
            str(workspace),
            "--env-file",
            str(env_file),
            "--backend",
            "claude-code",
            "--tailnet-provider",
            "netmesh",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Setup complete." in result.output
    assert "networking choice was saved" in result.output
    assert calls == [
        "thread-sync",
        "sounds",
        "claude-md",
        "provision-netmesh",
        "configure",
    ]

    calls.clear()
    result = runner.invoke(
        setup_cli.setup,
        [
            "--workspace-dir",
            str(workspace),
            "--env-file",
            str(env_file),
            "--backend",
            "claude-code",
            "--tailnet-provider",
            "netmesh-tsnet",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        "thread-sync",
        "sounds",
        "claude-md",
        "tunneld-binary",
        "service:openbase-tunneld",
        "configure",
    ]

    def unavailable_tunneld(_config):
        raise RuntimeError("Go is unavailable")

    _patch_setup(monkeypatch, "install_tunneld_binary", unavailable_tunneld)
    calls.clear()
    result = runner.invoke(
        setup_cli.setup,
        [
            "--workspace-dir",
            str(workspace),
            "--env-file",
            str(env_file),
            "--backend",
            "claude-code",
            "--tailnet-provider",
            "netmesh-tsnet",
        ],
    )

    assert result.exit_code != 0
    assert "Openbase VPN daemon installation failed: Go is unavailable" in result.output
    assert "configure" not in calls


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


def test_init_cli_workspace_retains_selected_local_audio_extra(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    cli_dir = workspace / "cli"
    cli_dir.mkdir(parents=True)
    commands = []

    _patch_setup(monkeypatch, "which", lambda _name: "uv")
    _patch_setup(
        monkeypatch,
        "_download_livekit_model_files",
        lambda *_args, **_kwargs: None,
    )

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    _patch_setup(monkeypatch, "subprocess", SimpleNamespace(run=fake_run))

    setup_cli._init_cli_workspace(str(workspace), include_local_audio=True)

    assert commands == [
        (
            ["uv", "sync", "--extra", "local-audio"],
            {"cwd": str(cli_dir), "check": True},
        )
    ]


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


def test_require_tailnet_provider_choice_restores_existing_netmesh(
    tmp_path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENBASE_CODER_CLI_TAILSCALE_PROVIDER=netmesh\n",
        encoding="utf-8",
    )

    assert (
        setup_cli._require_tailnet_provider_choice(
            str(env_file), None, interactive=False
        )
        == "netmesh"
    )


def test_require_tailnet_provider_choice_defaults_legacy_env_to_tailscale(
    tmp_path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=codex\n", encoding="utf-8")

    assert (
        setup_cli._require_tailnet_provider_choice(
            str(env_file), None, interactive=False
        )
        == "tailscale"
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
