from __future__ import annotations

import json
from pathlib import Path

from openbase_coder_cli import claude_plugins


def _configure_managed_config(monkeypatch, tmp_path: Path) -> Path:
    config_path = tmp_path / ".claude.json"
    monkeypatch.setattr(claude_plugins, "CLAUDE_STATE_PATH", config_path)
    return config_path


def test_enable_adds_entry_and_preserves_other_servers(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = _configure_managed_config(monkeypatch, tmp_path)
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {"super-agents": {"type": "stdio", "command": "x"}},
                "other": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claude_plugins,
        "openbase_coder_command_path",
        lambda: Path("/opt/openbase/bin/openbase-coder"),
    )

    assert claude_plugins.computer_use_enabled() is False
    assert claude_plugins.set_computer_use_enabled(True) is True
    assert claude_plugins.computer_use_enabled() is True

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["other"] is True
    assert payload["mcpServers"]["super-agents"] == {
        "type": "stdio",
        "command": "x",
    }
    assert payload["mcpServers"]["openbase-computer-use"] == {
        "type": "stdio",
        "command": "/opt/openbase/bin/openbase-coder",
        "args": ["claude", "computer-use-mcp"],
    }

    # Re-enabling with an identical entry is a no-op.
    assert claude_plugins.set_computer_use_enabled(True) is False


def test_disable_removes_entry(monkeypatch, tmp_path: Path) -> None:
    config_path = _configure_managed_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        claude_plugins,
        "openbase_coder_command_path",
        lambda: Path("/opt/openbase/bin/openbase-coder"),
    )

    assert claude_plugins.set_computer_use_enabled(False) is False
    assert claude_plugins.set_computer_use_enabled(True) is True
    assert claude_plugins.set_computer_use_enabled(False) is True
    assert claude_plugins.computer_use_enabled() is False

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["mcpServers"] == {}


def test_enable_creates_missing_config_file(monkeypatch, tmp_path: Path) -> None:
    config_path = _configure_managed_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        claude_plugins,
        "openbase_coder_command_path",
        lambda: Path("/opt/openbase/bin/openbase-coder"),
    )

    assert claude_plugins.set_computer_use_enabled(True) is True
    assert config_path.is_file()
    mode = config_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_chrome_toggle_writes_extra_args_env(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=claude-code\n", encoding="utf-8")
    monkeypatch.setattr(claude_plugins, "DEFAULT_ENV_FILE_PATH", env_file)

    assert claude_plugins.chrome_enabled() is False
    assert claude_plugins.set_chrome_enabled(True) is True
    assert claude_plugins.chrome_enabled() is True
    assert claude_plugins.set_chrome_enabled(True) is False

    content = env_file.read_text(encoding="utf-8")
    assert "OPENBASE_CODING_BACKEND=claude-code" in content
    assert "SUPER_AGENTS_CLAUDE_EXTRA_ARGS" in content
    from openbase_coder_cli.env_file import env_file_values

    assert json.loads(env_file_values(env_file)["SUPER_AGENTS_CLAUDE_EXTRA_ARGS"]) == {
        "chrome": None
    }


def test_chrome_disable_preserves_other_extra_args(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        'SUPER_AGENTS_CLAUDE_EXTRA_ARGS="{\\"chrome\\": null, \\"max-turns\\": \\"5\\"}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_plugins, "DEFAULT_ENV_FILE_PATH", env_file)

    assert claude_plugins.chrome_enabled() is True
    assert claude_plugins.set_chrome_enabled(False) is True
    assert claude_plugins.chrome_enabled() is False

    from openbase_coder_cli.env_file import env_file_values

    remaining = json.loads(env_file_values(env_file)["SUPER_AGENTS_CLAUDE_EXTRA_ARGS"])
    assert remaining == {"max-turns": "5"}


def test_chrome_toggle_creates_missing_env_file(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / "missing" / ".env"
    env_file.parent.mkdir()
    monkeypatch.setattr(claude_plugins, "DEFAULT_ENV_FILE_PATH", env_file)

    assert claude_plugins.set_chrome_enabled(False) is False
    assert claude_plugins.set_chrome_enabled(True) is True
    assert env_file.is_file()
