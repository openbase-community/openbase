from __future__ import annotations

import importlib.resources as importlib_resources
import json
import os
import subprocess
from pathlib import Path

import click
import pytest

from openbase_coder_cli.cli.setup import hooks
from openbase_coder_cli.paths import INJECT_SESSION_ID_HOOK_PATH


def test_hook_script_injects_session_id_with_usage_instructions() -> None:
    # The hook carries its own usage instructions so they ship, update, and
    # uninstall with it — no AGENTS.md edit is needed for the normal case.
    script = importlib_resources.files(hooks.BUNDLED_HOOKS_PACKAGE).joinpath(
        hooks.SESSION_ID_HOOK_FILENAME
    )
    with importlib_resources.as_file(script) as script_path:
        result = subprocess.run(
            ["bash", str(script_path)],
            input=json.dumps({"session_id": "abc-123"}),
            capture_output=True,
            text=True,
            check=True,
        )
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "abc-123" in context
    assert "Agent-Thread-Id" in context
    assert "do not query Super Agents" in context


def test_hook_script_stays_silent_without_session_id() -> None:
    script = importlib_resources.files(hooks.BUNDLED_HOOKS_PACKAGE).joinpath(
        hooks.SESSION_ID_HOOK_FILENAME
    )
    with importlib_resources.as_file(script) as script_path:
        result = subprocess.run(
            ["bash", str(script_path)],
            input="{}",
            capture_output=True,
            text=True,
            check=True,
        )
    assert result.stdout == ""


def test_hook_script_exports_session_id_for_claude_bash_commands(
    tmp_path: Path,
) -> None:
    script = importlib_resources.files(hooks.BUNDLED_HOOKS_PACKAGE).joinpath(
        hooks.SESSION_ID_HOOK_FILENAME
    )
    claude_env_file = tmp_path / "claude-session-env"
    session_id = "abc-123"

    with importlib_resources.as_file(script) as script_path:
        subprocess.run(
            ["bash", str(script_path)],
            input=json.dumps({"session_id": session_id}),
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "CLAUDE_ENV_FILE": str(claude_env_file)},
        )

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; printf "%s" "$OPENBASE_AGENT_ID"',
            "bash",
            str(claude_env_file),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == session_id


def test_trusted_hash_matches_codex_fingerprint() -> None:
    # Known-answer test against codex's normalized hook trust hash
    # (codex-rs/config/src/fingerprint.rs): this is the [hooks.state] value
    # codex 0.144.1 records for the Warp plugin's SessionStart hook. If this
    # breaks after a codex upgrade, the fingerprint scheme changed and
    # session_start_hook_trusted_hash must be updated to match.
    assert hooks.session_start_hook_trusted_hash(
        "${PLUGIN_ROOT}/scripts/on-session-start.sh"
    ) == ("sha256:91587043033e7831d4d154fdef2e495f3113c3552a4529d8013f5546ffb2c140")


def test_merge_claude_hooks_adds_entry_and_preserves_existing() -> None:
    existing = {
        "PostToolUse": [
            {"matcher": "Edit", "hooks": [{"type": "command", "command": "x"}]}
        ],
        "SessionStart": [
            {"matcher": "", "hooks": [{"type": "command", "command": "other.sh"}]}
        ],
    }

    merged = hooks.merge_session_id_hook_into_claude_hooks(existing)

    assert merged["PostToolUse"] == existing["PostToolUse"]
    session_start = merged["SessionStart"]
    assert len(session_start) == 2
    assert session_start[0]["hooks"][0]["command"] == "other.sh"
    assert session_start[1]["hooks"][0]["command"] == str(INJECT_SESSION_ID_HOOK_PATH)


def test_merge_claude_hooks_is_idempotent() -> None:
    once = hooks.merge_session_id_hook_into_claude_hooks(None)
    twice = hooks.merge_session_id_hook_into_claude_hooks(once)
    assert twice == once
    assert len(twice["SessionStart"]) == 1


def test_ensure_claude_session_id_hook_preserves_other_settings(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "model": "sonnet",
                "hooks": {"PostToolUse": [{"hooks": [{"command": "audit.sh"}]}]},
            }
        ),
        encoding="utf-8",
    )

    assert hooks.ensure_claude_session_id_hook(settings) is True
    updated = json.loads(settings.read_text(encoding="utf-8"))

    assert updated["model"] == "sonnet"
    assert updated["hooks"]["PostToolUse"] == [
        {"hooks": [{"command": "audit.sh"}]}
    ]
    assert updated["hooks"]["SessionStart"][0]["hooks"][0]["command"] == str(
        INJECT_SESSION_ID_HOOK_PATH
    )
    assert settings.stat().st_mode & 0o777 == 0o600
    assert hooks.ensure_claude_session_id_hook(settings) is False


def test_ensure_claude_session_id_hook_refuses_invalid_json(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(click.ClickException, match="Could not read Claude settings"):
        hooks.ensure_claude_session_id_hook(settings)

    assert settings.read_text(encoding="utf-8") == "not-json\n"


def test_ensure_codex_session_id_hook_appends_and_preserves(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'sandbox_mode = "danger-full-access"\n'
        "\n"
        "[mcp_servers.super-agents]\n"
        'command = "/bin/super-agents-mcp"\n'
        "\n"
        '[hooks.state."warp@codex-warp:hooks/hooks.json:session_start:0:0"]\n'
        'trusted_hash = "sha256:unrelated"\n',
        encoding="utf-8",
    )

    assert hooks.ensure_codex_session_id_hook(config) is True
    text = config.read_text(encoding="utf-8")

    assert 'sandbox_mode = "danger-full-access"' in text
    assert "[mcp_servers.super-agents]" in text
    assert 'trusted_hash = "sha256:unrelated"' in text
    assert "[[hooks.SessionStart]]" in text
    assert f'command = "{INJECT_SESSION_ID_HOOK_PATH}"' in text
    state_key = f"{config.parent.resolve() / config.name}:session_start:0:0"
    assert f'[hooks.state."{state_key}"]' in text
    assert "enabled = true" in text


def test_ensure_codex_session_id_hook_is_idempotent(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('model = "openbase-codex"\n', encoding="utf-8")

    assert hooks.ensure_codex_session_id_hook(config) is True
    first = config.read_text(encoding="utf-8")
    assert hooks.ensure_codex_session_id_hook(config) is False
    assert config.read_text(encoding="utf-8") == first
    assert first.count("[[hooks.SessionStart]]") == 1


def test_ensure_codex_session_id_hook_preserves_unrelated_session_hook(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    state_key = f"{config.parent.resolve() / config.name}:session_start:0:0"
    config.write_text(
        "[[hooks.SessionStart]]\n"
        "\n"
        "[[hooks.SessionStart.hooks]]\n"
        'type = "command"\n'
        'command = "/old/path.sh"\n'
        "\n"
        f'[hooks.state."{state_key}"]\n'
        'trusted_hash = "sha256:stale"\n'
        "enabled = true\n",
        encoding="utf-8",
    )

    assert hooks.ensure_codex_session_id_hook(config) is True
    text = config.read_text(encoding="utf-8")

    assert '/old/path.sh' in text
    assert 'sha256:stale' in text
    assert text.count("[[hooks.SessionStart]]") == 2
    managed_state_key = f"{config.parent.resolve() / config.name}:session_start:1:0"
    assert text.count(f'[hooks.state."{state_key}"]') == 1
    assert text.count(f'[hooks.state."{managed_state_key}"]') == 1


def test_ensure_codex_session_id_hook_reuses_managed_group_after_user_hook(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    command = str(INJECT_SESSION_ID_HOOK_PATH)
    config.write_text(
        "[[hooks.SessionStart]]\n\n"
        "[[hooks.SessionStart.hooks]]\n"
        'type = "command"\n'
        'command = "user-hook.sh"\n\n'
        "[[hooks.SessionStart]]\n\n"
        "[[hooks.SessionStart.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(command)}\n\n"
        '[hooks.state."stale-source:session_start:0:0"]\n'
        f"trusted_hash = {json.dumps(hooks.session_start_hook_trusted_hash(command))}\n"
        "enabled = true\n",
        encoding="utf-8",
    )

    assert hooks.ensure_codex_session_id_hook(config) is True
    text = config.read_text(encoding="utf-8")

    assert text.count("[[hooks.SessionStart]]") == 2
    assert text.count(f"command = {json.dumps(command)}") == 1
    assert "stale-source" not in text
    state_key = f"{config.parent.resolve() / config.name}:session_start:1:0"
    assert f'[hooks.state."{state_key}"]' in text
