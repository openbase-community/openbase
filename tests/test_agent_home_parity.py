"""Tests for codex/claude parity: shared-home MCP registration."""

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


def test_ensure_codex_config_adds_table_without_permission_values(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('model_reasoning_effort = "high"\n', encoding="utf-8")
    monkeypatch.setattr(codex_phase, "CODEX_CONFIG_PATH", config_path)
    monkeypatch.setattr(
        codex_phase, "ensure_codex_session_id_hook", lambda _path: False
    )
    command = _stub_super_agents_command(monkeypatch, codex_phase)

    codex_phase._ensure_codex_config(str(tmp_path / "workspace"))

    content = config_path.read_text(encoding="utf-8")
    assert "[mcp_servers.super-agents]" in content
    assert json.dumps(str(command)) in content
    assert 'model_reasoning_effort = "high"' in content
    # The Openbase permission posture is per-session env, never config values.
    assert 'SUPER_AGENTS_CODEX_APPROVAL_POLICY = "never"' in content
    assert 'SUPER_AGENTS_CODEX_SANDBOX_POLICY = "danger-full-access"' in content
    assert "sandbox_mode" not in content
    assert "approval_policy" not in content


def test_ensure_codex_config_is_idempotent(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(codex_phase, "CODEX_CONFIG_PATH", config_path)
    monkeypatch.setattr(
        codex_phase, "ensure_codex_session_id_hook", lambda _path: False
    )
    _stub_super_agents_command(monkeypatch, codex_phase)

    codex_phase._ensure_codex_config("")
    first = config_path.read_text(encoding="utf-8")
    codex_phase._ensure_codex_config("")

    assert config_path.read_text(encoding="utf-8") == first


def test_ensure_claude_mcp_adds_entry_and_preserves_state(
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
    monkeypatch.setattr(claude_phase, "CLAUDE_STATE_PATH", state_path)
    command = _stub_super_agents_command(monkeypatch, claude_phase)

    claude_phase._ensure_claude_mcp("")

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["hasCompletedOnboarding"] is True
    assert payload["mcpServers"]["existing"] == {"command": "existing"}
    entry = payload["mcpServers"]["super-agents"]
    assert entry["type"] == "stdio"
    assert entry["command"] == str(command)
    assert entry["env"]["SUPER_AGENTS_DEFAULT_BACKEND"] == "claude_code"
    assert "SUPER_AGENTS_BASE_INSTRUCTIONS_PATH" in entry["env"]
    # Sessions run against the shared ~/.claude; never redirect the config dir.
    assert "CLAUDE_CONFIG_DIR" not in entry["env"]


def test_ensure_claude_mcp_is_idempotent(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / ".claude.json"
    monkeypatch.setattr(claude_phase, "CLAUDE_STATE_PATH", state_path)
    _stub_super_agents_command(monkeypatch, claude_phase)

    claude_phase._ensure_claude_mcp("")
    first = state_path.read_text(encoding="utf-8")
    claude_phase._ensure_claude_mcp("")

    assert state_path.read_text(encoding="utf-8") == first
