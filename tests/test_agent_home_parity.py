"""Tests for codex/claude parity: normal-home MCP registration and the
Claude keychain auth bridge."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from openbase_coder_cli import claude_auth
from openbase_coder_cli.cli.setup import claude as claude_phase
from openbase_coder_cli.cli.setup import codex as codex_phase


def _stub_super_agents_command(monkeypatch, module) -> Path:
    command = Path("/opt/fake/super-agents-mcp")
    monkeypatch.setattr(
        module, "_super_agents_mcp_command", lambda _workspace: (command, [])
    )
    return command


def test_ensure_normal_codex_mcp_adds_only_the_table(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('model_reasoning_effort = "high"\n', encoding="utf-8")
    monkeypatch.setattr(codex_phase, "NORMAL_CODEX_CONFIG_PATH", config_path)
    command = _stub_super_agents_command(monkeypatch, codex_phase)

    codex_phase._ensure_normal_codex_mcp(str(tmp_path / "workspace"))

    content = config_path.read_text(encoding="utf-8")
    assert "[mcp_servers.super-agents]" in content
    assert json.dumps(str(command)) in content
    assert 'model_reasoning_effort = "high"' in content
    assert 'env = { SUPER_AGENTS_DEFAULT_BACKEND = "codex" }' in content
    # Never the Openbase permission overrides.
    assert "danger-full-access" not in content
    assert "approval_policy" not in content


def test_ensure_normal_codex_mcp_is_idempotent(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(codex_phase, "NORMAL_CODEX_CONFIG_PATH", config_path)
    _stub_super_agents_command(monkeypatch, codex_phase)

    codex_phase._ensure_normal_codex_mcp("")
    first = config_path.read_text(encoding="utf-8")
    codex_phase._ensure_normal_codex_mcp("")

    assert config_path.read_text(encoding="utf-8") == first


def test_ensure_normal_claude_mcp_adds_entry_and_preserves_state(
    tmp_path, monkeypatch
) -> None:
    state_path = tmp_path / ".claude.json"
    state_path.write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "mcpServers": {"existing": {"command": "existing"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_phase, "NORMAL_CLAUDE_STATE_PATH", state_path)
    command = _stub_super_agents_command(monkeypatch, claude_phase)

    claude_phase._ensure_normal_claude_mcp("")

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["hasCompletedOnboarding"] is True
    assert payload["mcpServers"]["existing"] == {"command": "existing"}
    assert payload["mcpServers"]["super-agents"] == {
        "type": "stdio",
        "command": str(command),
        "env": {"SUPER_AGENTS_DEFAULT_BACKEND": "claude_code"},
    }
    # Normal-home entry identifies Claude but never redirects CLAUDE_CONFIG_DIR.
    assert "CLAUDE_CONFIG_DIR" not in payload["mcpServers"]["super-agents"]["env"]


def test_copy_normal_claude_keychain_copies_secret(monkeypatch, tmp_path) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[1] == "find-generic-password" and "-w" in command:
            return subprocess.CompletedProcess(command, 0, stdout="secret-token\n")
        if command[1] == "find-generic-password":
            return subprocess.CompletedProcess(
                command, 0, stdout='    "acct"<blob>="user@example.com"\n'
            )
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(claude_auth.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(claude_auth.subprocess, "run", fake_run)
    config_dir = tmp_path / "claude_config"

    assert claude_auth.copy_normal_claude_keychain(config_dir=config_dir) is True

    add_command = commands[-1]
    assert add_command[:2] == ["security", "add-generic-password"]
    assert claude_auth.openbase_claude_keychain_service(config_dir) in add_command
    assert "user@example.com" in add_command
    assert "secret-token" in add_command


def test_copy_normal_claude_keychain_skips_when_no_source(monkeypatch) -> None:
    monkeypatch.setattr(claude_auth.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        claude_auth.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 44, stdout=""),
    )

    assert claude_auth.copy_normal_claude_keychain() is False
