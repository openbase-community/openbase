"""argv/env construction for `openbase-coder codex|claude` (agent_launch)."""

from __future__ import annotations

from pathlib import Path

import pytest

from openbase_coder_cli.agent_launch import (
    AgentLaunchError,
    LaunchContext,
    codex_profile_name,
    is_interactive_session,
    plan_agent_launch,
)


@pytest.fixture
def homes(tmp_path):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "openbase.config.toml").write_text('model = "gpt-5.5"\n')
    (codex_home / "openbase-cloud.config.toml").write_text('model = "gpt-5.5"\n')
    claude = tmp_path / "claude-profile"
    claude.mkdir()
    (claude / "settings.json").write_text("{}")
    (claude / "mcp.json").write_text('{"mcpServers": {}}')
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("Openbase base instructions.\n")
    return {
        "codex_home": codex_home,
        "settings": claude / "settings.json",
        "mcp": claude / "mcp.json",
        "instructions": instructions,
    }


def _context(homes, *, backend="codex", endpoint="unix://", **overrides):
    values = {
        "backend": backend,
        "find_binary": lambda name: Path(f"/opt/bin/{name}"),
        "codex_endpoint": lambda: endpoint,
        "codex_home": homes["codex_home"],
        "claude_settings_path": homes["settings"],
        "claude_mcp_path": homes["mcp"],
        "base_instructions_path": homes["instructions"],
        "cloud_codex_api_key": lambda: "machine-token",
        "cloud_claude_env": lambda backend: {
            "ANTHROPIC_BASE_URL": "https://cloud/anthropic",
            "ANTHROPIC_AUTH_TOKEN": "machine-token",
        },
        "cloud_claude_model": lambda model, backend: model or "claude-haiku-4-5",
    }
    values.update(overrides)
    return LaunchContext(**values)


def _plan(agent, args, homes, cwd="/work/app", base_env=None, **context):
    return plan_agent_launch(
        agent,
        args,
        cwd,
        _context(homes, **context),
        base_env=base_env if base_env is not None else {"PATH": "/usr/bin"},
    )


# --- Codex -----------------------------------------------------------------


def test_codex_new_session_attaches_to_the_managed_app_server_in_cwd(homes):
    launch = _plan("codex", ["fix the build"], homes)

    assert launch.argv == [
        "/opt/bin/codex",
        "-p",
        "openbase",
        "--remote",
        "unix://",
        "-C",
        "/work/app",
        "fix the build",
    ]
    assert launch.cwd == "/work/app"
    assert launch.attached is True
    assert launch.notices == ()


def test_codex_keeps_an_explicit_cd(homes):
    launch = _plan("codex", ["-C", "/elsewhere"], homes)

    assert launch.argv.count("-C") == 1
    assert launch.argv[-2:] == ["-C", "/elsewhere"]


def test_codex_resume_puts_remote_after_the_subcommand(homes):
    launch = _plan("codex", ["resume", "--last"], homes)

    assert launch.argv == [
        "/opt/bin/codex",
        "-p",
        "openbase",
        "resume",
        "--remote",
        "unix://",
        "--last",
    ]
    assert launch.attached is True


def test_codex_non_interactive_subcommands_only_get_the_profile(homes):
    launch = _plan("codex", ["exec", "summarize"], homes)

    assert launch.argv == ["/opt/bin/codex", "-p", "openbase", "exec", "summarize"]
    assert launch.attached is False


def test_codex_user_endpoint_is_not_overridden(homes):
    launch = _plan("codex", ["--remote", "ws://box:4500"], homes)

    assert launch.argv == [
        "/opt/bin/codex",
        "-p",
        "openbase",
        "--remote",
        "ws://box:4500",
    ]


def test_codex_without_app_server_runs_standalone_with_a_notice(homes):
    launch = _plan("codex", [], homes, endpoint=None)

    assert launch.argv == ["/opt/bin/codex", "-p", "openbase"]
    assert launch.attached is False
    assert len(launch.notices) == 1
    assert "not running" in launch.notices[0]


@pytest.mark.parametrize("backend", ["openbase_cloud", "openbase_cloud_codex"])
def test_codex_cloud_backend_uses_cloud_profile_standalone(homes, backend):
    launch = _plan("codex", ["hi"], homes, backend=backend)

    assert codex_profile_name(backend) == "openbase-cloud"
    assert launch.argv == ["/opt/bin/codex", "-p", "openbase-cloud", "hi"]
    assert launch.env["OPENBASE_CLOUD_CODEX_API_KEY"] == "machine-token"
    assert launch.attached is False
    assert any("standalone" in notice for notice in launch.notices)


def test_codex_cloud_keeps_an_existing_api_key(homes):
    launch = _plan(
        "codex",
        [],
        homes,
        backend="openbase_cloud_codex",
        base_env={"OPENBASE_CLOUD_CODEX_API_KEY": "mine"},
        cloud_codex_api_key=lambda: pytest.fail("must not mint a token"),
    )

    assert launch.env["OPENBASE_CLOUD_CODEX_API_KEY"] == "mine"


def test_codex_missing_profile_is_an_error(homes):
    (homes["codex_home"] / "openbase.config.toml").unlink()

    with pytest.raises(AgentLaunchError, match="profiles install"):
        _plan("codex", [], homes)


def test_missing_binary_is_an_error(homes):
    with pytest.raises(AgentLaunchError, match="not installed"):
        _plan("codex", [], homes, find_binary=lambda name: None)


def test_launch_env_drops_enclosing_agent_session_markers(homes):
    env = {
        "PATH": "/usr/bin",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SESSION_ID": "parent",
        "CODEX_THREAD_ID": "parent",
        "HOME": "/Users/me",
    }

    for agent in ("codex", "claude"):
        launch = _plan(agent, [], homes, base_env=env)
        assert "CLAUDECODE" not in launch.env
        assert "CLAUDE_CODE_SESSION_ID" not in launch.env
        assert "CODEX_THREAD_ID" not in launch.env
        assert launch.env["HOME"] == "/Users/me"


# --- Claude Code -------------------------------------------------------------


def test_claude_session_gets_settings_mcp_and_instructions(homes):
    launch = _plan(
        "claude",
        ["-c"],
        homes,
        base_env={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-shell"},
    )

    assert launch.argv == [
        "/opt/bin/claude",
        "--settings",
        str(homes["settings"]),
        "--mcp-config",
        str(homes["mcp"]),
        "--append-system-prompt",
        "Openbase base instructions.",
        "-c",
    ]
    assert "ANTHROPIC_API_KEY" not in launch.env
    assert launch.attached is True


def test_claude_respects_a_user_append_system_prompt(homes):
    launch = _plan("claude", ["--append-system-prompt", "mine"], homes)

    assert launch.argv.count("--append-system-prompt") == 1
    assert launch.argv[-2:] == ["--append-system-prompt", "mine"]


def test_claude_without_instructions_file_skips_the_prompt(homes):
    launch = _plan(
        "claude", [], homes, base_instructions_path=homes["settings"].parent / "nope"
    )

    assert "--append-system-prompt" not in launch.argv


def test_claude_cloud_backend_routes_through_openbase_cloud(homes):
    launch = _plan("claude", [], homes, backend="openbase_cloud")

    assert launch.env["ANTHROPIC_BASE_URL"] == "https://cloud/anthropic"
    assert launch.argv[-2:] == ["--model", "claude-haiku-4-5"]


def test_claude_cloud_backend_keeps_the_users_model(homes):
    launch = _plan("claude", ["--model", "opus"], homes, backend="openbase_cloud")

    assert launch.argv.count("--model") == 1
    assert launch.argv[-2:] == ["--model", "opus"]


def test_claude_management_subcommands_run_plain(homes):
    launch = _plan("claude", ["mcp", "list"], homes)

    assert launch.argv == ["/opt/bin/claude", "mcp", "list"]
    assert launch.attached is False


def test_claude_missing_profile_is_an_error(homes):
    homes["mcp"].unlink()

    with pytest.raises(AgentLaunchError, match="profiles install"):
        _plan("claude", [], homes)


def test_unknown_agent_is_an_error(homes):
    with pytest.raises(AgentLaunchError):
        _plan("gemini", [], homes)


@pytest.mark.parametrize(
    ("agent", "args", "expected"),
    [
        ("codex", [], True),
        ("codex", ["fix it"], True),
        ("codex", ["resume", "--last"], True),
        ("codex", ["exec", "x"], False),
        ("codex", ["--remote", "ws://x"], False),
        ("codex", ["--no-daemon"], False),
        ("claude", [], True),
        ("claude", ["-p", "x"], True),
        ("claude", ["mcp", "list"], False),
    ],
)
def test_interactive_session_detection(agent, args, expected):
    assert is_interactive_session(agent, args) is expected
