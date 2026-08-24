from __future__ import annotations

from openbase_coder_cli.services import runners


def test_livekit_server_local_mode_binds_loopback(monkeypatch):
    monkeypatch.setattr(runners.platform, "system", lambda: "Linux")
    env = {
        "LIVEKIT_NETWORK_MODE": "local",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "secret",
    }
    binaries = {"livekit": "/usr/local/bin/livekit-server"}

    argv, out_env = runners.build_livekit_server(env, binaries)

    assert argv[0] == "/usr/local/bin/livekit-server"
    assert "--dev" in argv
    assert argv[argv.index("--bind") + 1] == "127.0.0.1"
    assert argv[argv.index("--node-ip") + 1] == "127.0.0.1"
    assert argv[argv.index("--keys") + 1] == "key: secret"
    config_body = argv[argv.index("--config-body") + 1]
    assert "interfaces:" in config_body
    assert "lo" in config_body


def test_livekit_server_tailscale_mode_resolves_node_ip_and_interface(monkeypatch):
    monkeypatch.setattr(runners.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(runners.network, "tailscale_ip", lambda family: "100.64.1.2")
    monkeypatch.setattr(runners.network, "resolve_interface", lambda ip: "en0")
    env = {
        "LIVEKIT_NETWORK_MODE": "tailscale",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "secret",
    }
    binaries = {"livekit": "/usr/local/bin/livekit-server"}

    argv, _ = runners.build_livekit_server(env, binaries)

    assert argv[argv.index("--node-ip") + 1] == "100.64.1.2"
    config_body = argv[argv.index("--config-body") + 1]
    assert "en0" in config_body
    assert "100.64.1.2/32" in config_body


def test_livekit_server_tailscale_mode_exits_without_node_ip(monkeypatch):
    monkeypatch.setattr(runners.platform, "system", lambda: "Linux")
    monkeypatch.setattr(runners.network, "tailscale_ip", lambda family: None)
    env = {"LIVEKIT_NETWORK_MODE": "tailscale"}
    binaries = {"livekit": "/usr/local/bin/livekit-server"}

    try:
        runners.build_livekit_server(env, binaries)
        raised = False
    except SystemExit:
        raised = True
    assert raised


def test_codex_app_server_builds_default_backend_argv(tmp_path, monkeypatch):
    from openbase_coder_cli import paths

    codex_home = tmp_path / ".codex"
    monkeypatch.setattr(paths, "CODEX_HOME_DIR", codex_home)
    # A stray CODEX_HOME in the service environment must not retarget the
    # service away from the shared ~/.codex home.
    env: dict[str, str] = {"CODEX_HOME": "/somewhere/else"}
    binaries = {
        "codex": "/usr/local/bin/codex",
        "openbase_coder": "/usr/local/bin/openbase-coder",
    }

    argv, out_env = runners.build_codex_app_server(env, binaries)

    assert argv[0] == "/usr/local/bin/codex"
    assert argv[1] == "app-server"
    assert argv[2:8] == [
        "-c",
        'model_reasoning_effort="high"',
        "-c",
        'service_tier="standard"',
        "-c",
        'model="gpt-5.5"',
    ]
    assert argv[-2:] == ["--listen", "ws://127.0.0.1:4500"]
    assert out_env["CODEX_HOME"] == str(codex_home)
    assert out_env["DISABLE_AUTOUPDATER"] == "1"
    assert codex_home.is_dir()


def test_codex_app_server_fetches_cloud_token_for_cloud_backend(tmp_path, monkeypatch):
    from openbase_coder_cli import paths

    monkeypatch.setattr(paths, "CODEX_HOME_DIR", tmp_path / ".codex")

    class _Result:
        returncode = 0
        stdout = "cloud-token-value\n"

    monkeypatch.setattr(runners.subprocess, "run", lambda *a, **k: _Result())
    env = {"OPENBASE_CODING_BACKEND": "openbase_cloud_codex"}
    binaries = {
        "codex": "/usr/local/bin/codex",
        "openbase_coder": "/usr/local/bin/openbase-coder",
    }

    argv, out_env = runners.build_codex_app_server(env, binaries)

    assert out_env["OPENBASE_CLOUD_CODEX_API_KEY"] == "cloud-token-value"
    assert 'model="gpt-5.5"' in argv
    assert 'model_provider="openbase_cloud"' in argv
    assert 'model_providers.openbase_cloud.name="Openbase Cloud"' in argv
    assert any(
        arg.startswith('model_providers.openbase_cloud.base_url="') for arg in argv
    )
    assert (
        'model_providers.openbase_cloud.env_key="OPENBASE_CLOUD_CODEX_API_KEY"' in argv
    )
    assert 'model_providers.openbase_cloud.wire_api="responses"' in argv


def test_sync_workers_argv():
    argv, env = runners.build_sync_workers(
        {}, {"openbase_coder": "/bin/openbase-coder"}
    )

    assert argv == ["/bin/openbase-coder", "sync-workers", "run"]


def test_openbase_routines_default_interval():
    argv, _ = runners.build_openbase_routines(
        {}, {"openbase_coder": "/bin/openbase-coder"}
    )

    assert argv == [
        "/bin/openbase-coder",
        "routines",
        "run-loop",
        "--interval",
        "60",
    ]


def test_openbase_routines_custom_interval():
    env = {"OPENBASE_CODER_ROUTINES_INTERVAL": "30"}
    argv, _ = runners.build_openbase_routines(
        env, {"openbase_coder": "/bin/openbase-coder"}
    )

    assert argv[-1] == "30"


def test_livekit_agent_tailscale_mode_sets_livekit_url(monkeypatch):
    monkeypatch.setattr(runners.network, "tailscale_ip", lambda family: "100.64.1.2")
    env = {"LIVEKIT_NETWORK_MODE": "tailscale"}
    binaries = {"python": "/opt/openbase/python"}

    argv, out_env = runners.build_livekit_agent(env, binaries)

    assert argv == [
        "/opt/openbase/python",
        "-m",
        "openbase_coder_cli.livekit_agent.livekit",
        "start",
    ]
    assert out_env["LIVEKIT_URL"] == "ws://localhost:7880"
    assert out_env["LIVEKIT_NODE_IP"] == "100.64.1.2"
    assert out_env["LIVEKIT_AGENT_LOAD_THRESHOLD"] == "2.0"


def test_livekit_agent_unsupported_mode_exits(monkeypatch):
    monkeypatch.setattr(runners.network, "tailscale_ip", lambda family: None)
    env = {"LIVEKIT_NETWORK_MODE": "bogus"}
    binaries = {"python": "/opt/openbase/python"}

    try:
        runners.build_livekit_agent(env, binaries)
        raised = False
    except SystemExit:
        raised = True
    assert raised


def test_django_cli_tailscale_mode_derives_livekit_url(monkeypatch):
    monkeypatch.setattr(runners.network, "tailscale_ip", lambda family: "100.64.1.2")
    env = {"LIVEKIT_NETWORK_MODE": "tailscale"}
    binaries = {"openbase_coder": "/bin/openbase-coder"}

    argv, out_env = runners.build_django_cli(env, binaries)

    assert out_env["LIVEKIT_URL"] == "ws://100.64.1.2:7880"
    assert argv == [
        "/bin/openbase-coder",
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        "7999",
    ]


def test_django_cli_local_mode_preserves_custom_url(monkeypatch):
    monkeypatch.setattr(runners.network, "tailscale_ip", lambda family: None)
    env = {"LIVEKIT_NETWORK_MODE": "local", "LIVEKIT_URL": "ws://custom:9999"}
    binaries = {"openbase_coder": "/bin/openbase-coder"}

    _, out_env = runners.build_django_cli(env, binaries)

    assert out_env["LIVEKIT_URL"] == "ws://custom:9999"


def test_django_cli_lan_mode_uses_default_lan_ip(monkeypatch):
    monkeypatch.setattr(runners.network, "tailscale_ip", lambda family: None)
    monkeypatch.setattr(runners.network, "default_lan_ip", lambda: "192.168.1.20")
    env = {"LIVEKIT_NETWORK_MODE": "lan"}
    binaries = {"openbase_coder": "/bin/openbase-coder"}

    _, out_env = runners.build_django_cli(env, binaries)

    assert out_env["LIVEKIT_URL"] == "ws://192.168.1.20:7880"


def test_code_sync_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "OPENBASE_BASE_DIR", tmp_path)
    argv, _ = runners.build_code_sync({}, {"syncthing": "/bin/syncthing"})

    assert argv == [
        "/bin/syncthing",
        "serve",
        "--home",
        str(tmp_path / "code-sync"),
        "--no-browser",
        "--no-restart",
        "--no-upgrade",
    ]


def test_openbase_cloud_auth_rehydrate_argv():
    argv, _ = runners.build_openbase_cloud_auth_rehydrate(
        {}, {"openbase_coder": "/bin/openbase-coder"}
    )

    assert argv == ["/bin/openbase-coder", "cloud", "rehydrate-auth"]


def test_openbase_cloud_heartbeat_rehydrates_then_builds_heartbeat_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(runners.subprocess, "run", lambda argv, **k: calls.append(argv))
    env = {"OPENBASE_CLOUD_HEARTBEAT_INTERVAL": "45"}
    binaries = {"openbase_coder": "/bin/openbase-coder"}

    argv, _ = runners.build_openbase_cloud_heartbeat(env, binaries)

    assert calls == [["/bin/openbase-coder", "cloud", "rehydrate-auth"]]
    assert argv == [
        "/bin/openbase-coder",
        "cloud",
        "heartbeat",
        "--interval",
        "45",
    ]


def test_runner_registry_covers_every_service():
    from openbase_coder_cli.services.definitions import SERVICES

    for svc in SERVICES:
        assert svc.command_template in runners.RUNNERS, svc.name


def test_load_env_merges_env_file_over_process_env(tmp_path, monkeypatch):
    from openbase_coder_cli.services.installation import InstallationConfig

    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVEKIT_NETWORK_MODE=lan\nOPENBASE_CODER_ROUTINES_INTERVAL=30\n"
    )
    monkeypatch.setattr(
        runners.os, "environ", {"LIVEKIT_NETWORK_MODE": "tailscale", "PATH": "/bin"}
    )
    config = InstallationConfig(env_file=str(env_file))

    env = runners._load_env(config)

    # File values override whatever the parent process already had, matching
    # the bash wrapper's ``set -a; source "$env_file"`` semantics.
    assert env["LIVEKIT_NETWORK_MODE"] == "lan"
    assert env["OPENBASE_CODER_ROUTINES_INTERVAL"] == "30"
    assert env["PATH"] == "/bin"


def test_load_env_without_env_file_returns_process_env(monkeypatch):
    from openbase_coder_cli.services.installation import InstallationConfig

    monkeypatch.setattr(runners.os, "environ", {"PATH": "/bin"})
    config = InstallationConfig(env_file="")

    assert runners._load_env(config) == {"PATH": "/bin"}
