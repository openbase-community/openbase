import importlib
from types import SimpleNamespace

from click.testing import CliRunner

from openbase_coder_cli.cli.doctor import (
    _check_agent_auth,
    _check_livekit_client_credentials,
    _check_local_livekit_tcp_listener,
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


def test_local_livekit_tcp_check_accepts_disabled_listener():
    messages = []

    _check_local_livekit_tcp_listener(
        {"LIVEKIT_NETWORK_MODE": "local"},
        [("127.0.0.1", 7880)],
        lambda message: messages.append(("ok", message)),
        lambda message: messages.append(("fail", message)),
    )

    assert messages == [
        ("ok", "port 7881 (LiveKit ICE-TCP): disabled in local Netmesh mode")
    ]


def test_local_livekit_tcp_check_rejects_any_listener():
    messages = []

    _check_local_livekit_tcp_listener(
        {"LIVEKIT_NETWORK_MODE": "local"},
        [("*", 7881)],
        lambda message: messages.append(("ok", message)),
        lambda message: messages.append(("fail", message)),
    )

    assert messages == [
        (
            "fail",
            "port 7881 (LiveKit ICE-TCP): must not listen in local Netmesh "
            "mode (found *)",
        )
    ]


def test_livekit_tcp_check_leaves_kernel_vpn_mode_unchanged():
    messages = []

    _check_local_livekit_tcp_listener(
        {"LIVEKIT_NETWORK_MODE": "tailscale"},
        [("*", 7881)],
        lambda message: messages.append(("ok", message)),
        lambda message: messages.append(("fail", message)),
    )

    assert messages == []


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


def _patch_doctor_runtime(monkeypatch, tmp_path, services, launchctl_status):
    """A healthy codex-backend install whose service list is ``services``."""
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
    monkeypatch.setattr(doctor_cli, "SERVICES", services)
    monkeypatch.setattr(doctor_cli, "launchctl_status", launchctl_status)
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
        "_check_sync_daemon",
        lambda ok, _warn, _fail: ok("Openbase Sync: healthy"),
    )
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        doctor_cli.Path,
        "home",
        classmethod(lambda cls: tmp_path),
    )
    monkeypatch.setattr(doctor_cli, "CODEX_HOME_DIR", codex_home)
    _patch_agent_home_paths(monkeypatch, tmp_path)


def test_doctor_allows_optional_stopped_services(monkeypatch, tmp_path):
    _patch_doctor_runtime(
        monkeypatch,
        tmp_path,
        [
            SimpleNamespace(
                name="codex-thread-device-sync",
                install_by_default=False,
                supports_backend=lambda _backend: True,
            )
        ],
        lambda _svc: {"installed": True, "pid": None, "last_exit_code": None},
    )

    result = CliRunner().invoke(doctor_cli.doctor)

    assert result.exit_code == 0, result.output
    assert "codex-thread-device-sync: optional (not running" in result.output


def test_doctor_reports_shared_daemon_and_cli_upgrade_over_idle_runner(
    monkeypatch, tmp_path
):
    from openbase_coder_cli.services.codex_version_skew import CodexVersionSkew

    _patch_doctor_runtime(
        monkeypatch,
        tmp_path,
        [
            SimpleNamespace(
                name="codex-app-server",
                install_by_default=True,
                supports_backend=lambda _backend: True,
            )
        ],
        # Openbase's runner idles behind the daemon and holds a pid.
        lambda _svc: {"installed": True, "pid": 4242, "last_exit_code": 0},
    )
    monkeypatch.setattr(
        "openbase_coder_cli.codex_control_plane.shared_codex_daemon_ready",
        lambda: True,
    )
    monkeypatch.setattr(
        "openbase_coder_cli.services.codex_version_skew.service_version_skew",
        lambda name: CodexVersionSkew(
            service=name,
            running_version="0.161.0",
            installed_version="0.160.1",
            installed_path="/opt/codex",
            shared_daemon=True,
        ),
    )

    result = CliRunner().invoke(doctor_cli.doctor)

    assert result.exit_code == 0, result.output
    assert "codex-app-server: available through the shared Codex daemon" in result.output
    assert "upgrade the Codex CLI to 0.161.0" in result.output
    assert "pid 4242" not in result.output


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
    monkeypatch.setattr(
        doctor_cli, "configured_coding_backends", lambda: ["claude_code"]
    )
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
    from openbase_coder_cli.services import tailnet_experience

    monkeypatch.setattr(tailnet_experience.tp, "provider", lambda: "tailscale")
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
        "_check_sync_daemon",
        lambda ok, _warn, _fail: ok("Openbase Sync: healthy"),
    )
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
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
    assert "SETUP Official Tailscale: control tool not found" in result.output
    assert "setup actions" in result.output
    assert "FAIL" not in result.output


def _collect_sync_daemon_check(monkeypatch, *, plan_has_legacy=False):
    from openbase_coder_cli import sync_migration

    class _Plan:
        has_legacy_state = plan_has_legacy

    monkeypatch.setattr(sync_migration, "plan_migration", lambda **_: _Plan())
    oks: list[str] = []
    warns: list[str] = []
    fails: list[str] = []
    doctor_cli._check_sync_daemon(oks.append, warns.append, fails.append)
    return oks, warns, fails


def test_check_sync_daemon_unconfigured_is_ok(monkeypatch):
    oks, warns, fails = _collect_sync_daemon_check(monkeypatch)

    assert oks == ["Openbase Sync: not configured"]
    assert warns == [] and fails == []


def test_check_sync_daemon_flags_leftover_previous_sync(monkeypatch):
    _oks, warns, _fails = _collect_sync_daemon_check(monkeypatch, plan_has_legacy=True)

    assert any("migrate-from-syncthing" in message for message in warns)


def _write_daemon_config(tmp_path):
    from openbase_coder_cli import sync_daemon

    config = sync_daemon.SyncDaemonConfig(
        device_id="laptop",
        sync_group="default",
        role="edge",
        pair_secret="s",
        roots=[{"id": "projects", "path": str(tmp_path / "Projects")}],
        peer_hot="hub:22100",
        peer_bulk="hub:22101",
    )
    sync_daemon.write_config(config)


def test_check_sync_daemon_fails_when_service_missing(monkeypatch, tmp_path):
    _write_daemon_config(tmp_path)
    monkeypatch.setattr(doctor_cli, "launchctl_status", lambda svc: {"installed": False})

    _oks, _warns, fails = _collect_sync_daemon_check(monkeypatch)

    assert any("sync-daemon service: not installed" in message for message in fails)


def test_check_sync_daemon_reports_peers_and_conflicts(monkeypatch, tmp_path):
    from openbase_coder_cli import sync_daemon

    _write_daemon_config(tmp_path)
    monkeypatch.setattr(
        doctor_cli, "launchctl_status", lambda svc: {"installed": True, "pid": "42"}
    )
    monkeypatch.setattr(
        sync_daemon.SyncDaemonClient,
        "status",
        lambda self: {"peers": [{"device": "mini"}], "open_conflicts": 2},
    )

    oks, warns, fails = _collect_sync_daemon_check(monkeypatch)

    assert fails == []
    assert oks == ["Openbase Sync: edge, 1 root(s), 1 peer(s) connected"]
    assert any("2 open conflict(s)" in message for message in warns)


def _patch_agent_home_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor_cli, "CODEX_PROFILE_PATH", tmp_path / "codex" / "openbase.config.toml"
    )
    monkeypatch.setattr(
        doctor_cli, "CLAUDE_PROFILE_MCP_PATH", tmp_path / ".claude.json"
    )
    monkeypatch.setattr(doctor_cli, "CLAUDE_CONFIG_DIR", tmp_path / "claude_config")
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

    codex_config = tmp_path / "codex" / "openbase.config.toml"
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
        level == "fail" and "Codex config" in message and str(gone) in message
        for level, message in messages
    )
    assert any(
        level == "ok" and "Claude config" in message for level, message in messages
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
        and "Claude config" in message
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


def _collect_service_runtime_paths(monkeypatch, wrapper_dir, current_dir):
    messages = []

    def ok(message):
        messages.append(("ok", message))

    def warn(message):
        messages.append(("warn", message))

    def fail(message):
        messages.append(("fail", message))

    monkeypatch.setattr(doctor_cli, "LAUNCHD_WRAPPER_DIR", wrapper_dir)
    monkeypatch.setattr(doctor_cli, "STANDALONE_CURRENT_DIR", current_dir)
    doctor_cli._check_service_runtime_paths(ok, warn, fail)
    return messages


def _make_package(root, *, with_interpreter=True):
    (root / "bin").mkdir(parents=True, exist_ok=True)
    binary = root / "bin" / "openbase-coder"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    if with_interpreter:
        (root / "python" / "bin").mkdir(parents=True, exist_ok=True)
        interpreter = root / "python" / "bin" / "python"
        interpreter.write_text("#!/bin/sh\n")
        interpreter.chmod(0o755)
    return root


def _write_wrapper(wrapper_dir, name, target):
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper = wrapper_dir / f"{name}.sh"
    wrapper.write_text(f'#!/bin/bash\ncd "/tmp"\nexec {target} server --port 7999\n')
    return wrapper


def test_service_runtime_paths_ok_when_wrappers_resolve(monkeypatch, tmp_path):
    package = _make_package(tmp_path / "release")
    current = tmp_path / "current"
    current.symlink_to(package)
    wrappers = tmp_path / "launchd"
    _write_wrapper(wrappers, "django-cli", current / "bin" / "openbase-coder")

    messages = _collect_service_runtime_paths(monkeypatch, wrappers, current)

    assert not [m for m in messages if m[0] == "fail"]
    assert any("service wrappers resolve" in m[1] for m in messages)


def test_service_runtime_paths_flags_dangling_wrapper_target(monkeypatch, tmp_path):
    package = _make_package(tmp_path / "release")
    current = tmp_path / "current"
    current.symlink_to(package)
    wrappers = tmp_path / "launchd"
    _write_wrapper(wrappers, "livekit-agent", tmp_path / "gone" / "python")

    messages = _collect_service_runtime_paths(monkeypatch, wrappers, current)

    failures = [m[1] for m in messages if m[0] == "fail"]
    assert any("livekit-agent" in f for f in failures)
    assert any("services regenerate" in f for f in failures)


def test_service_runtime_paths_flags_package_missing_interpreter(monkeypatch, tmp_path):
    """The exact production failure: 'current' resolves, but its Python does not.

    The wrapper execs bin/openbase-coder, which exists and is executable, so
    checking only the wrapper target reports healthy. The launcher then execs
    the package interpreter, whose symlink dangles into a renamed app bundle,
    and every service dies on start.
    """
    package = _make_package(tmp_path / "release", with_interpreter=False)
    (package / "python" / "bin").mkdir(parents=True)
    (package / "python" / "bin" / "python").symlink_to(tmp_path / "renamed-away")
    current = tmp_path / "current"
    current.symlink_to(package)
    wrappers = tmp_path / "launchd"
    _write_wrapper(wrappers, "django-cli", current / "bin" / "openbase-coder")

    messages = _collect_service_runtime_paths(monkeypatch, wrappers, current)

    failures = [m[1] for m in messages if m[0] == "fail"]
    assert any("no usable interpreter" in f for f in failures)


def test_service_runtime_paths_flags_dangling_current_alias(monkeypatch, tmp_path):
    current = tmp_path / "current"
    current.symlink_to(tmp_path / "deleted-release")
    wrappers = tmp_path / "launchd"
    _write_wrapper(wrappers, "django-cli", current / "bin" / "openbase-coder")

    messages = _collect_service_runtime_paths(monkeypatch, wrappers, current)

    failures = [m[1] for m in messages if m[0] == "fail"]
    assert any("dangling" in f for f in failures)


def test_service_runtime_paths_silent_without_wrapper_dir(monkeypatch, tmp_path):
    messages = _collect_service_runtime_paths(
        monkeypatch, tmp_path / "absent", tmp_path / "current"
    )
    assert messages == []
