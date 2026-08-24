import importlib
from types import SimpleNamespace

from click.testing import CliRunner

from openbase_coder_cli.cli.doctor import (
    _check_agent_auth,
    _check_livekit_client_credentials,
)

doctor_cli = importlib.import_module("openbase_coder_cli.cli.doctor")


def _collect_credential_check(env):
    messages = []

    def warn(message):
        messages.append(("warn", message))

    def ok(message):
        messages.append(("ok", message))

    _check_livekit_client_credentials(env, warn, ok)
    return messages


def _collect_auth_check(env, monkeypatch, tmp_path, cloud_login=None):
    messages = []

    def ok(message):
        messages.append(("ok", message))

    def warn(message):
        messages.append(("warn", message))

    def fail(message):
        messages.append(("fail", message))

    def action(message):
        messages.append(("action", message))

    monkeypatch.setattr(
        doctor_cli.Path,
        "home",
        classmethod(lambda cls: tmp_path),
    )
    from openbase_coder_cli.services import onboarding as onboarding_module

    monkeypatch.setattr(
        onboarding_module,
        "cloud_login_status",
        lambda: (
            cloud_login or {"status": "logged_out", "validated": True, "detail": ""}
        ),
    )
    monkeypatch.setattr(doctor_cli, "CODEX_HOME_DIR", tmp_path / "codex_home")
    _check_agent_auth(env, ok, warn, fail, action)
    return messages


def test_livekit_client_credential_check_warns_when_missing():
    messages = _collect_credential_check(
        {
            "LIVEKIT_API_KEY": "server-key",
            "LIVEKIT_API_SECRET": "server-secret",
        }
    )

    assert messages == [
        (
            "warn",
            "LiveKit client token credentials missing "
            "(LIVEKIT_CLIENT_API_KEY, LIVEKIT_CLIENT_API_SECRET): "
            "run 'openbase-coder setup' and restart services",
        )
    ]


def test_livekit_client_credential_check_warns_when_reusing_server_credentials():
    messages = _collect_credential_check(
        {
            "LIVEKIT_API_KEY": "same-key",
            "LIVEKIT_API_SECRET": "same-secret",
            "LIVEKIT_CLIENT_API_KEY": "same-key",
            "LIVEKIT_CLIENT_API_SECRET": "same-secret",
        }
    )

    assert messages == [
        (
            "warn",
            "LiveKit client token credentials reuse local server credentials "
            "(LIVEKIT_CLIENT_API_KEY, LIVEKIT_CLIENT_API_SECRET): "
            "run 'openbase-coder setup' and restart services",
        )
    ]


def test_livekit_client_credential_check_accepts_separate_credentials():
    messages = _collect_credential_check(
        {
            "LIVEKIT_API_KEY": "server-key",
            "LIVEKIT_API_SECRET": "server-secret",
            "LIVEKIT_CLIENT_API_KEY": "client-key",
            "LIVEKIT_CLIENT_API_SECRET": "client-secret",
        }
    )

    assert messages == [
        (
            "ok",
            "LiveKit client token credentials: set and separate from server credentials",
        )
    ]


def test_agent_auth_requires_codex_login_for_codex_backend(monkeypatch, tmp_path):
    messages = _collect_auth_check(
        {"OPENBASE_CODING_BACKEND": "codex"}, monkeypatch, tmp_path
    )

    assert ("action", "Codex auth missing: run 'codex login'") in messages


def test_agent_auth_requires_openbase_login_for_cloud_backend(monkeypatch, tmp_path):
    messages = _collect_auth_check(
        {"OPENBASE_CODING_BACKEND": "openbase_cloud"}, monkeypatch, tmp_path
    )

    assert (
        "action",
        "Openbase Cloud auth missing: run 'openbase-coder login'",
    ) in messages


def test_agent_auth_reports_expired_openbase_login_for_cloud_backend(
    monkeypatch, tmp_path
):
    messages = _collect_auth_check(
        {"OPENBASE_CODING_BACKEND": "openbase_cloud"},
        monkeypatch,
        tmp_path,
        cloud_login={"status": "login_expired", "validated": True, "detail": ""},
    )

    assert (
        "action",
        "Openbase Cloud login expired or was revoked: run 'openbase-coder login' again",
    ) in messages


def test_agent_auth_accepts_validated_openbase_login_for_cloud_backend(
    monkeypatch, tmp_path
):
    messages = _collect_auth_check(
        {"OPENBASE_CODING_BACKEND": "openbase_cloud"},
        monkeypatch,
        tmp_path,
        cloud_login={"status": "logged_in", "validated": True, "detail": ""},
    )

    assert ("ok", "Openbase Cloud auth: logged in") in messages


def test_agent_auth_requires_claude_login_for_claude_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor_cli,
        "claude_auth_status",
        lambda: SimpleNamespace(logged_in=False, raw_output="", returncode=1),
    )

    messages = _collect_auth_check(
        {"OPENBASE_CODING_BACKEND": "claude_code"}, monkeypatch, tmp_path
    )

    assert (
        "action",
        "Claude Code auth missing: run 'claude auth login'",
    ) in messages


def test_doctor_allows_optional_stopped_services(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODER_CLI_SECRET_KEY=x\n", encoding="utf-8")
    monkeypatch.setattr(doctor_cli.InstallationConfig, "exists", lambda: True)
    monkeypatch.setattr(
        doctor_cli.InstallationConfig,
        "load",
        lambda: SimpleNamespace(
            standalone=False,
            workspace_path=str(tmp_path),
        ),
    )
    monkeypatch.setattr(doctor_cli, "configured_coding_backends", lambda: ["codex"])
    monkeypatch.setattr(
        doctor_cli,
        "SERVICES",
        [
            SimpleNamespace(
                name="codex-thread-device-sync",
                install_by_default=False,
                supports_backend=lambda _backend: True,
            )
        ],
    )
    monkeypatch.setattr(
        doctor_cli,
        "launchctl_status",
        lambda _svc: {"installed": True, "pid": None, "last_exit_code": None},
    )
    monkeypatch.setattr(
        doctor_cli,
        "_get_listening_sockets",
        lambda: [("127.0.0.1", 7999), ("127.0.0.1", 7880)],
    )
    monkeypatch.setattr(doctor_cli, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        doctor_cli,
        "_parse_env_file",
        lambda: {
            "OPENBASE_CODER_CLI_SECRET_KEY": "secret",
            "LIVEKIT_API_KEY": "server-key",
            "LIVEKIT_API_SECRET": "server-secret",
            "LIVEKIT_CLIENT_API_KEY": "client-key",
            "LIVEKIT_CLIENT_API_SECRET": "client-secret",
        },
    )
    monkeypatch.setattr(
        doctor_cli,
        "tailscale_serve_health",
        lambda: SimpleNamespace(
            tailscale_available=True,
            tailscale_running=True,
            host="mac.tailnet.ts.net",
            openbase_url="http://mac.tailnet.ts.net:18080",
            openbase_configured=True,
            livekit_configured=True,
            openbase_reachable=True,
            error=None,
        ),
    )
    monkeypatch.setattr(doctor_cli, "selected_tts_provider_id", lambda: "cartesia")
    monkeypatch.setattr(doctor_cli, "selected_stt_provider_id", lambda: "assemblyai")
    monkeypatch.setattr(
        doctor_cli,
        "_check_code_sync",
        lambda ok, _warn, _fail: ok("code sync: healthy"),
    )
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        doctor_cli.Path,
        "home",
        classmethod(lambda cls: tmp_path),
    )
    monkeypatch.setattr(doctor_cli, "CODEX_HOME_DIR", codex_home)
    _patch_agent_home_paths(monkeypatch, tmp_path)

    result = CliRunner().invoke(doctor_cli.doctor)

    assert result.exit_code == 0, result.output
    assert "codex-thread-device-sync: optional (not running" in result.output


def test_doctor_skips_backend_scoped_services_on_other_backends(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODER_CLI_SECRET_KEY=x\n", encoding="utf-8")
    monkeypatch.setattr(doctor_cli.InstallationConfig, "exists", lambda: True)
    monkeypatch.setattr(
        doctor_cli.InstallationConfig,
        "load",
        lambda: SimpleNamespace(
            standalone=False,
            workspace_path=str(tmp_path),
            package_path="",
            python_path="",
            livekit_server_path="",
            console_build_dir="",
        ),
    )
    monkeypatch.setattr(doctor_cli, "configured_coding_backends", lambda: ["claude_code"])
    monkeypatch.setattr(
        doctor_cli,
        "SERVICES",
        [
            SimpleNamespace(
                name="codex-app-server",
                install_by_default=True,
                supports_backend=lambda backend: (
                    backend in ("codex", "openbase_cloud_codex")
                ),
            )
        ],
    )
    monkeypatch.setattr(
        doctor_cli,
        "launchctl_status",
        lambda _svc: {"installed": False, "pid": None, "last_exit_code": None},
    )
    monkeypatch.setattr(
        doctor_cli,
        "_get_listening_sockets",
        lambda: [("127.0.0.1", 7999), ("127.0.0.1", 7880)],
    )
    monkeypatch.setattr(doctor_cli, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        doctor_cli,
        "_parse_env_file",
        lambda: {
            "OPENBASE_CODER_CLI_SECRET_KEY": "secret",
            "LIVEKIT_API_KEY": "server-key",
            "LIVEKIT_API_SECRET": "server-secret",
            "LIVEKIT_CLIENT_API_KEY": "client-key",
            "LIVEKIT_CLIENT_API_SECRET": "client-secret",
            "OPENBASE_CODING_BACKEND": "claude_code",
        },
    )
    monkeypatch.setattr(
        doctor_cli,
        "tailscale_serve_health",
        lambda: SimpleNamespace(
            tailscale_available=True,
            tailscale_running=True,
            host="mac.tailnet.ts.net",
            openbase_url="http://mac.tailnet.ts.net:18080",
            openbase_configured=True,
            livekit_configured=True,
            openbase_reachable=True,
            error=None,
        ),
    )
    monkeypatch.setattr(doctor_cli, "selected_tts_provider_id", lambda: "cartesia")
    monkeypatch.setattr(doctor_cli, "selected_stt_provider_id", lambda: "assemblyai")
    monkeypatch.setattr(
        doctor_cli,
        "claude_auth_status",
        lambda: SimpleNamespace(logged_in=True, raw_output="", returncode=0),
    )
    monkeypatch.setattr(
        doctor_cli.Path,
        "home",
        classmethod(lambda cls: tmp_path),
    )

    result = CliRunner().invoke(doctor_cli.doctor)

    assert result.exit_code == 0, result.output
    assert "codex-app-server: not used (claude_code backend)" in result.output
    assert "codex-app-server: not installed" not in result.output


def test_doctor_reports_missing_tailscale_as_setup_action(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODER_CLI_SECRET_KEY=x\n", encoding="utf-8")
    monkeypatch.setattr(doctor_cli.InstallationConfig, "exists", lambda: True)
    monkeypatch.setattr(
        doctor_cli.InstallationConfig,
        "load",
        lambda: SimpleNamespace(
            standalone=False,
            workspace_path=str(tmp_path),
        ),
    )
    monkeypatch.setattr(doctor_cli, "configured_coding_backends", lambda: ["codex"])
    monkeypatch.setattr(doctor_cli, "SERVICES", [])
    monkeypatch.setattr(doctor_cli, "_get_listening_sockets", lambda: [])
    monkeypatch.setattr(doctor_cli, "DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(
        doctor_cli,
        "_parse_env_file",
        lambda: {
            "OPENBASE_CODER_CLI_SECRET_KEY": "secret",
            "LIVEKIT_API_KEY": "server-key",
            "LIVEKIT_API_SECRET": "server-secret",
            "LIVEKIT_CLIENT_API_KEY": "client-key",
            "LIVEKIT_CLIENT_API_SECRET": "client-secret",
        },
    )
    monkeypatch.setattr(
        doctor_cli,
        "tailscale_serve_health",
        lambda: SimpleNamespace(
            tailscale_available=False,
            tailscale_running=False,
            host=None,
            openbase_url=None,
            openbase_configured=False,
            livekit_configured=False,
            openbase_reachable=False,
            error="tailscale was not found on PATH.",
        ),
    )
    monkeypatch.setattr(doctor_cli, "selected_tts_provider_id", lambda: "cartesia")
    monkeypatch.setattr(doctor_cli, "selected_stt_provider_id", lambda: "assemblyai")
    monkeypatch.setattr(
        doctor_cli,
        "_check_code_sync",
        lambda ok, _warn, _fail: ok("code sync: healthy"),
    )
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        doctor_cli.Path,
        "home",
        classmethod(lambda cls: tmp_path),
    )
    monkeypatch.setattr(doctor_cli, "CODEX_HOME_DIR", codex_home)
    _patch_agent_home_paths(monkeypatch, tmp_path)

    result = CliRunner().invoke(doctor_cli.doctor)

    assert result.exit_code == 0, result.output
    assert "SETUP tailscale: not found on PATH" in result.output
    assert "setup actions" in result.output
    assert "FAIL" not in result.output


def test_stignore_content_follows_includes(tmp_path):
    (tmp_path / ".stglobalignore").write_text("// shared\n(?d).git\n", encoding="utf-8")
    stignore = tmp_path / ".stignore"
    stignore.write_text("#include .stglobalignore\n/foo/data\n", encoding="utf-8")

    content = doctor_cli._stignore_content_with_includes(stignore)

    assert "(?d).git" in content
    assert "/foo/data" in content


def test_check_code_sync_fails_when_managed_stignore_lacks_git(monkeypatch, tmp_path):
    from pathlib import Path

    from openbase_coder_cli.sync_config import SyncFolder

    folder = SyncFolder(relpath="Projects/demo")
    folder_root = tmp_path / "Projects" / "demo"
    folder_root.mkdir(parents=True)
    (folder_root / ".stignore").write_text("node_modules\n", encoding="utf-8")

    monkeypatch.setattr(doctor_cli, "_syncthing_process_running", lambda: False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "openbase_coder_cli.sync_config.code_sync_enabled", lambda: True
    )
    monkeypatch.setattr(
        "openbase_coder_cli.sync_config.sync_folders", lambda: (folder,)
    )
    monkeypatch.setattr(
        doctor_cli, "launchctl_status", lambda service: {"installed": True, "pid": 1}
    )
    monkeypatch.setattr(
        "openbase_coder_cli.code_sync.manager.versions_usage_bytes", lambda: 0
    )

    failures: list[str] = []
    doctor_cli._check_code_sync(lambda msg: None, lambda msg: None, failures.append)

    assert any("no .git ignore" in message for message in failures)


def _patch_agent_home_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor_cli, "NORMAL_CODEX_CONFIG_PATH", tmp_path / "codex" / "config.toml"
    )
    monkeypatch.setattr(
        doctor_cli, "NORMAL_CLAUDE_STATE_PATH", tmp_path / ".claude.json"
    )
    monkeypatch.setattr(
        doctor_cli,
        "OPENBASE_CLAUDE_JSON_PATH",
        tmp_path / "claude_config" / ".claude.json",
    )
    monkeypatch.setattr(
        doctor_cli, "OPENBASE_CLAUDE_CONFIG_DIR", tmp_path / "claude_config"
    )
    monkeypatch.setattr(doctor_cli, "STANDALONE_RELEASES_DIR", tmp_path / "releases")


def _collect_agent_home_messages(check, monkeypatch, tmp_path):
    messages = []

    def ok(message):
        messages.append(("ok", message))

    def warn(message):
        messages.append(("warn", message))

    def fail(message):
        messages.append(("fail", message))

    _patch_agent_home_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor_cli, "CODEX_HOME_DIR", tmp_path / "codex_home")
    check(ok, warn, fail)
    return messages


def test_mcp_registration_check_fails_on_dangling_command(monkeypatch, tmp_path):
    import json as json_module

    codex_config = tmp_path / "codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    gone = tmp_path / "releases" / "0.1.0" / "super-agents-mcp"
    codex_config.write_text(
        f"[mcp_servers.super-agents]\ncommand = {json_module.dumps(str(gone))}\n",
        encoding="utf-8",
    )
    live = tmp_path / "bin" / "super-agents-mcp"
    live.parent.mkdir(parents=True)
    live.write_text("#!/bin/sh\n", encoding="utf-8")
    claude_state = tmp_path / ".claude.json"
    claude_state.write_text(
        json_module.dumps({"mcpServers": {"super-agents": {"command": str(live)}}}),
        encoding="utf-8",
    )

    messages = _collect_agent_home_messages(
        doctor_cli._check_super_agents_mcp_registrations, monkeypatch, tmp_path
    )

    assert any(
        level == "fail" and "normal Codex config" in message and str(gone) in message
        for level, message in messages
    )
    assert any(
        level == "ok" and "normal Claude config" in message
        for level, message in messages
    )


def test_mcp_registration_check_warns_on_version_pinned_command(monkeypatch, tmp_path):
    import json as json_module

    pinned = tmp_path / "releases" / "1.0.0" / "super-agents-mcp"
    pinned.parent.mkdir(parents=True)
    pinned.write_text("#!/bin/sh\n", encoding="utf-8")
    claude_state = tmp_path / ".claude.json"
    claude_state.write_text(
        json_module.dumps({"mcpServers": {"super-agents": {"command": str(pinned)}}}),
        encoding="utf-8",
    )

    messages = _collect_agent_home_messages(
        doctor_cli._check_super_agents_mcp_registrations, monkeypatch, tmp_path
    )

    assert any(
        level == "warn"
        and "normal Claude config" in message
        and "pinned to versioned release" in message
        for level, message in messages
    )


def test_agent_home_skills_check_reports_dangling_and_healthy(monkeypatch, tmp_path):
    codex_skills = tmp_path / "codex_home" / "skills"
    codex_skills.mkdir(parents=True)
    (codex_skills / "broken-skill").symlink_to(tmp_path / "releases" / "0.1.0" / "gone")
    claude_skills = tmp_path / "claude_config" / "skills"
    claude_skills.mkdir(parents=True)
    healthy_source = tmp_path / "current" / "skills" / "good-skill"
    healthy_source.mkdir(parents=True)
    (claude_skills / "good-skill").symlink_to(healthy_source)
    (claude_skills / ".stignore").write_text("", encoding="utf-8")

    messages = _collect_agent_home_messages(
        doctor_cli._check_agent_home_skills, monkeypatch, tmp_path
    )

    assert any(
        level == "fail" and "broken-skill" in message for level, message in messages
    )
    assert any(
        level == "ok" and "1 entries resolve" in message for level, message in messages
    )
