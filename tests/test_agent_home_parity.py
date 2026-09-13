"""Tests for Codex and Claude Code session-profile parity."""

from __future__ import annotations

import json
from pathlib import Path

from openbase_coder_cli.cli.setup import claude as claude_phase
from openbase_coder_cli.cli.setup import codex as codex_phase


def _stub_super_agents_command(monkeypatch, module) -> Path:
    command = Path("/opt/fake/super-agents-mcp")
    monkeypatch.setattr(
        module, "_super_agents_mcp_command", lambda _workspace: (command, [])
    )
    return command


def test_ensure_codex_config_adds_openbase_profile_without_changing_default(
    tmp_path, monkeypatch
) -> None:
    default_config_path = tmp_path / "config.toml"
    profile_path = tmp_path / "openbase.config.toml"
    cloud_profile_path = tmp_path / "openbase-cloud.config.toml"
    default_config = 'model = "personal"\n'
    profile_path.write_text('model_reasoning_effort = "high"\n', encoding="utf-8")
    default_config_path.write_text(default_config, encoding="utf-8")
    monkeypatch.setattr(codex_phase, "CODEX_CONFIG_PATH", default_config_path)
    monkeypatch.setattr(codex_phase, "CODEX_PROFILE_PATH", profile_path)
    monkeypatch.setattr(codex_phase, "CLOUD_CODEX_PROFILE_PATH", cloud_profile_path)
    monkeypatch.setattr(
        codex_phase, "ensure_codex_session_id_hook", lambda _path: False
    )
    command = _stub_super_agents_command(monkeypatch, codex_phase)

    codex_phase._ensure_codex_config(str(tmp_path / "workspace"))

    content = profile_path.read_text(encoding="utf-8")
    assert "[mcp_servers.super-agents]" in content
    assert json.dumps(str(command)) in content
    assert 'model_reasoning_effort = "high"' in content
    assert 'SUPER_AGENTS_CODEX_APPROVAL_POLICY = "never"' in content
    assert 'SUPER_AGENTS_CODEX_SANDBOX_POLICY = "danger-full-access"' in content
    assert 'sandbox_mode = "danger-full-access"' in content
    assert 'approval_policy = "never"' in content
    assert default_config_path.read_text(encoding="utf-8") == default_config


def test_ensure_codex_config_is_idempotent(tmp_path, monkeypatch) -> None:
    default_config_path = tmp_path / "config.toml"
    profile_path = tmp_path / "openbase.config.toml"
    cloud_profile_path = tmp_path / "openbase-cloud.config.toml"
    monkeypatch.setattr(codex_phase, "CODEX_CONFIG_PATH", default_config_path)
    monkeypatch.setattr(codex_phase, "CODEX_PROFILE_PATH", profile_path)
    monkeypatch.setattr(codex_phase, "CLOUD_CODEX_PROFILE_PATH", cloud_profile_path)
    monkeypatch.setattr(
        codex_phase, "ensure_codex_session_id_hook", lambda _path: False
    )
    _stub_super_agents_command(monkeypatch, codex_phase)

    codex_phase._ensure_codex_config("")
    first = (
        profile_path.read_text(encoding="utf-8"),
        cloud_profile_path.read_text(encoding="utf-8"),
    )
    codex_phase._ensure_codex_config("")

    assert (
        profile_path.read_text(encoding="utf-8"),
        cloud_profile_path.read_text(encoding="utf-8"),
    ) == first


def test_ensure_claude_mcp_adds_entry_and_preserves_state(
    tmp_path, monkeypatch
) -> None:
    state_path = tmp_path / ".claude.json"
    profile_path = tmp_path / "mcp.json"
    state_path.write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "mcpServers": {"existing": {"command": "existing"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_phase, "CLAUDE_STATE_PATH", state_path)
    monkeypatch.setattr(claude_phase, "CLAUDE_PROFILE_MCP_PATH", profile_path)
    command = _stub_super_agents_command(monkeypatch, claude_phase)

    claude_phase._ensure_claude_mcp("")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["hasCompletedOnboarding"] is True
    assert state["mcpServers"] == {"existing": {"command": "existing"}}
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    entry = payload["mcpServers"]["super-agents"]
    assert entry["type"] == "stdio"
    assert entry["command"] == str(command)
    assert entry["env"]["SUPER_AGENTS_DEFAULT_BACKEND"] == "claude_code"
    assert "SUPER_AGENTS_BASE_INSTRUCTIONS_PATH" in entry["env"]
    # Sessions run against the shared ~/.claude; never redirect the config dir.
    assert "CLAUDE_CONFIG_DIR" not in entry["env"]


def test_ensure_claude_mcp_is_idempotent(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / ".claude.json"
    profile_path = tmp_path / "mcp.json"
    monkeypatch.setattr(claude_phase, "CLAUDE_STATE_PATH", state_path)
    monkeypatch.setattr(claude_phase, "CLAUDE_PROFILE_MCP_PATH", profile_path)
    _stub_super_agents_command(monkeypatch, claude_phase)

    claude_phase._ensure_claude_mcp("")
    first = profile_path.read_text(encoding="utf-8")
    claude_phase._ensure_claude_mcp("")

    assert profile_path.read_text(encoding="utf-8") == first
