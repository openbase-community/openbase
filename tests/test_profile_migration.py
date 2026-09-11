from __future__ import annotations

import json
import tomllib

from openbase_coder_cli.cli.setup import profile_migration as migration
from openbase_coder_cli.cli.setup.hooks import ensure_codex_session_id_hook


def test_config_update_preserves_user_dotfile_symlinks(tmp_path):
    target = tmp_path / "dotfile.json"
    target.write_text('{"model":"personal"}')
    link = tmp_path / "settings.json"
    link.symlink_to(target)
    migration.write_if_changed(link, '{"model":"preserved"}', backup=True)
    assert link.is_symlink()
    assert target.read_text() == '{"model":"preserved"}'
    assert (
        link.with_name("settings.json.before-openbase-profiles").read_text()
        == '{"model":"personal"}'
    )


def test_codex_migration_preserves_defaults_unrelated_mcp_and_hook_trust(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.toml"
    command = tmp_path / "inject-session-id.sh"
    monkeypatch.setattr(migration, "INJECT_SESSION_ID_HOOK_PATH", command)
    from openbase_coder_cli.cli.setup import hooks

    monkeypatch.setattr(hooks, "INJECT_SESSION_ID_HOOK_PATH", command)
    original = (
        '# User preferences\nmodel = "personal-model"\nmodel_reasoning_effort = "medium"\n'
        'sandbox_mode = "read-only"\napproval_policy = "on-request"\n'
        '[mcp_servers.personal]\ncommand = "personal-mcp"\n'
    )
    path.write_text(original)
    ensure_codex_session_id_hook(path)
    with path.open("a") as stream:
        stream.write(
            '[mcp_servers.super-agents]\ncommand = "super-agents-mcp"\n'
            '[mcp_servers.super-agents.env]\nSUPER_AGENTS_CODEX_SANDBOX_POLICY = "danger-full-access"\n'
            '[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\ntype = "command"\ncommand = "personal-hook"\n'
            f'[hooks.state.{json.dumps(str(path.resolve()) + ":session_start:1:0")}]\ntrusted_hash = "personal-hash"\nenabled = true\n'
        )
    before = path.read_bytes()
    migration.migrate_codex_user_config(path)
    after = path.read_bytes()
    config = tomllib.loads(after.decode())
    assert config["model"] == "personal-model"
    assert config["model_reasoning_effort"] == "medium"
    assert config["sandbox_mode"] == "read-only"
    assert config["approval_policy"] == "on-request"
    assert config["mcp_servers"] == {"personal": {"command": "personal-mcp"}}
    assert config["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "personal-hook"
    assert (
        config["hooks"]["state"][f"{path.resolve()}:session_start:0:0"]["trusted_hash"]
        == "personal-hash"
    )
    assert (
        path.with_name(path.name + ".before-openbase-profiles").read_bytes() == before
    )
    migration.migrate_codex_user_config(path)
    assert path.read_bytes() == after


def test_claude_migration_preserves_auth_preferences_and_other_hooks(
    tmp_path, monkeypatch
):
    state = tmp_path / ".claude.json"
    settings = tmp_path / "settings.json"
    command = tmp_path / "inject-session-id.sh"
    monkeypatch.setattr(migration, "INJECT_SESSION_ID_HOOK_PATH", command)
    state.write_text(
        json.dumps(
            {
                "oauthAccount": {"accountUuid": "existing-account"},
                "mcpServers": {
                    "personal": {"command": "personal-mcp"},
                    "super-agents": {
                        "command": "super-agents-mcp",
                        "env": {
                            "SUPER_AGENTS_BASE_INSTRUCTIONS_PATH": "instructions.md"
                        },
                    },
                },
            }
        )
    )
    settings.write_text(
        json.dumps(
            {
                "model": "opus",
                "effortLevel": "max",
                "permissions": {"defaultMode": "auto"},
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {"command": str(command)},
                                {"command": "personal-hook"},
                            ]
                        }
                    ]
                },
            }
        )
    )
    migration.migrate_claude_user_config(state, settings)
    assert json.loads(state.read_text()) == {
        "oauthAccount": {"accountUuid": "existing-account"},
        "mcpServers": {"personal": {"command": "personal-mcp"}},
    }
    assert json.loads(settings.read_text()) == {
        "model": "opus",
        "effortLevel": "max",
        "permissions": {"defaultMode": "auto"},
        "hooks": {"SessionStart": [{"hooks": [{"command": "personal-hook"}]}]},
    }
    before = (state.read_bytes(), settings.read_bytes())
    migration.migrate_claude_user_config(state, settings)
    assert before == (state.read_bytes(), settings.read_bytes())
