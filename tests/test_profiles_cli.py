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
        lambda *_args, **kwargs: events.append(
            ("codex-profile", kwargs.get("register_shared_super_agents"))
        ),
    )
    monkeypatch.setattr(
        profiles_module,
        "_ensure_claude_mcp",
        lambda *_args, **_kwargs: events.append("claude-mcp-profile"),
    )
    monkeypatch.setattr(
        profiles_module,
        "_ensure_claude_hooks",
        lambda **kwargs: events.append(
            ("claude-settings-profile", kwargs.get("register_shared_super_agents"))
        ),
    )
    monkeypatch.setattr(
        profiles_module,
        "upsert_env_file_values",
        lambda *_args, **_kwargs: events.append("profile-environment"),
    )
    monkeypatch.setattr(
        profiles_module,
        "ensure_default_session_id_hooks",
        lambda: events.append("default-hooks"),
    )


def test_profiles_install_includes_default_hooks_by_default(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[object] = []
    _patch_profile_install(monkeypatch, tmp_path, events)

    result = CliRunner().invoke(profiles_module.profiles, ["install"])

    assert result.exit_code == 0
    assert events[-1] == "default-hooks"


def test_profiles_install_registers_shared_super_agents_by_default(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[object] = []
    _patch_profile_install(monkeypatch, tmp_path, events)

    result = CliRunner().invoke(profiles_module.profiles, ["install"])

    assert result.exit_code == 0
    assert ("codex-profile", True) in events
    assert ("claude-settings-profile", True) in events


def test_profiles_install_can_opt_out_of_shared_super_agents(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[object] = []
    _patch_profile_install(monkeypatch, tmp_path, events)

    result = CliRunner().invoke(
        profiles_module.profiles,
        ["install", "--no-shared-super-agents-mcp"],
    )

    assert result.exit_code == 0
    assert ("codex-profile", False) in events
    assert ("claude-settings-profile", False) in events


def test_profiles_install_can_disable_default_hooks(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[object] = []
    _patch_profile_install(monkeypatch, tmp_path, events)

    result = CliRunner().invoke(
        profiles_module.profiles,
        ["install", "--no-include-default-hooks"],
    )

    assert result.exit_code == 0
    assert "default-hooks" not in events
