from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

profiles_module = importlib.import_module("openbase_coder_cli.cli.profiles")


def _patch_profile_install(monkeypatch, tmp_path: Path, events: list[object]) -> None:
    monkeypatch.setattr(
        profiles_module,
        "require_installation",
        lambda: SimpleNamespace(
            env_file=str(tmp_path / ".env"),
            workspace_path=str(tmp_path / "workspace"),
        ),
    )
    monkeypatch.setattr(
        profiles_module,
        "selected_backend_from_env_file",
        lambda _path: "codex",
    )
    monkeypatch.setattr(
        profiles_module,
        "ensure_session_id_hook_script",
        lambda: events.append("hook-script"),
    )
    monkeypatch.setattr(
        profiles_module,
        "_ensure_codex_config",
        lambda *_args, **_kwargs: events.append("codex-profile"),
    )
    monkeypatch.setattr(
        profiles_module,
        "_ensure_claude_mcp",
        lambda *_args, **_kwargs: events.append("claude-mcp-profile"),
    )
    monkeypatch.setattr(
        profiles_module,
        "_ensure_claude_hooks",
        lambda: events.append("claude-settings-profile"),
    )
    monkeypatch.setattr(
        profiles_module,
        "upsert_env_file_values",
        lambda *_args, **_kwargs: events.append("profile-environment"),
    )
    monkeypatch.setattr(
        profiles_module,
        "ensure_codex_session_id_hook",
        lambda path, *, backup=False: events.append(
            ("default-codex-hook", path, backup)
        ),
    )
    monkeypatch.setattr(
        profiles_module,
        "ensure_claude_session_id_hook",
        lambda path, *, backup=False: events.append(
            ("default-claude-hook", path, backup)
        ),
    )


def test_profiles_install_keeps_default_hooks_opt_in(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[object] = []
    _patch_profile_install(monkeypatch, tmp_path, events)

    result = CliRunner().invoke(profiles_module.profiles, ["install"])

    assert result.exit_code == 0
    assert not any(
        isinstance(event, tuple) and event[0].startswith("default-") for event in events
    )


def test_profiles_install_can_include_default_hooks(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[object] = []
    _patch_profile_install(monkeypatch, tmp_path, events)

    result = CliRunner().invoke(
        profiles_module.profiles,
        ["install", "--include-default-hooks"],
    )

    assert result.exit_code == 0
    assert events[-2:] == [
        ("default-codex-hook", profiles_module.CODEX_CONFIG_PATH, True),
        ("default-claude-hook", profiles_module.CLAUDE_SETTINGS_PATH, True),
    ]
