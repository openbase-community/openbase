from openbase_coder_cli.services.definitions import SERVICES


def test_livekit_server_service_supports_tailscale_and_local_modes():
    service = next(svc for svc in SERVICES if svc.name == "livekit-server")
    command = service.command_template.format(
        livekit="/usr/local/bin/livekit-server",
        data_dir="/tmp/openbase",
        workspace="/tmp/workspace",
    )

    assert 'LIVEKIT_NETWORK_MODE="${LIVEKIT_NETWORK_MODE:-tailscale}"' in command
    assert 'case "$LIVEKIT_NETWORK_MODE" in' in command
    assert "    local)" in command
    assert "    tailscale)" in command
    assert 'LIVEKIT_TCP_PORT="${LIVEKIT_TCP_PORT:-7881}"' in command
    assert (
        'LIVEKIT_NODE_IP_V6="$("$TAILSCALE_BIN" ip -6 2>/dev/null | head -n 1)"'
        in command
    )
    # App Store installs keep the tailscale CLI inside the app bundle.
    assert "/Applications/Tailscale.app/Contents/MacOS/Tailscale" in command
    assert "Ignoring invalid Tailscale IPv4 value: $LIVEKIT_NODE_IP" in command
    assert "Ignoring invalid Tailscale IPv6 value: $LIVEKIT_NODE_IP_V6" in command
    assert 'ifconfig 2>/dev/null | awk -v ip="$LIVEKIT_NODE_IP"' in command
    assert 'route -n get "$LIVEKIT_NODE_IP"' in command
    assert "%s\\n      - %s/128\\n" in command
    assert "tcp_port: %s" in command
    assert 'LIVEKIT_BIND_IP="${LIVEKIT_BIND_IP:-127.0.0.1}"' in command
    assert "enable_loopback_candidate: true" in command
    assert 'LIVEKIT_LOOPBACK_IFACE="lo0"' in command
    assert 'LIVEKIT_LOOPBACK_IFACE="lo"' in command
    assert 'ip -o -4 addr show 2>/dev/null | awk -v ip="$LIVEKIT_NODE_IP"' in command
    assert '"$LIVEKIT_LOOPBACK_IFACE"' in command
    assert 'LIVEKIT_KEYS="$LIVEKIT_API_KEY: $LIVEKIT_API_SECRET"' in command
    assert "LIVEKIT_CLIENT_API_KEY" in command
    assert '--keys "$LIVEKIT_KEYS"' in command
    assert '--bind "$LIVEKIT_BIND_IP"' in command
    assert service.cleanup_ports == (7880, 7881)


def test_codex_app_server_service_sets_model_defaults():
    service = next(svc for svc in SERVICES if svc.name == "codex-app-server")
    command = service.command_template.format(
        codex="/usr/local/bin/codex",
        openbase_coder="/usr/local/bin/openbase-coder",
        data_dir="/tmp/openbase",
        workspace="/tmp/workspace",
    )

    assert "OPENBASE_CODEX_BACKEND" not in command
    assert "claude-agent-sdk" not in command
    assert "bypasses codex-app-server" not in command
    assert "exec /usr/local/bin/codex app-server" in command
    assert "CODEX_CLAUDE_" not in command
    assert 'OPENBASE_CODING_BACKEND="${OPENBASE_CODING_BACKEND:-codex}"' in command
    assert "openbase_cloud_codex" in command
    assert "OPENBASE_CLOUD_CODEX_API_KEY" in command
    assert "auth print-machine-token" in command
    assert "auth print-access-token" not in command
    assert 'export DISABLE_AUTOUPDATER="${DISABLE_AUTOUPDATER:-1}"' in command
    assert "model_providers.openbase_cloud.base_url" not in command
    assert 'CODEX_MODEL="${CODEX_MODEL:-gpt-5.5}"' not in command
    assert (
        'CODEX_MODEL_REASONING_EFFORT="${CODEX_MODEL_REASONING_EFFORT:-high}"'
        in command
    )
    assert 'CODEX_SERVICE_TIER="${CODEX_SERVICE_TIER:-standard}"' in command
    assert '-c "model=\\"$CODEX_MODEL\\""' not in command
    assert '-c "model_reasoning_effort=\\"$CODEX_MODEL_REASONING_EFFORT\\""' in command
    assert '-c "service_tier=\\"$CODEX_SERVICE_TIER\\""' in command


def test_livekit_agent_service_does_not_export_dispatcher_instructions_path():
    service = next(svc for svc in SERVICES if svc.name == "livekit-agent")
    command = service.command_template.format(
        data_dir="/tmp/openbase",
        python="/opt/openbase/python/bin/python",
        runtime_workdir="/opt/openbase",
        workspace="/tmp/workspace",
    )

    assert 'LIVEKIT_NETWORK_MODE="${LIVEKIT_NETWORK_MODE:-tailscale}"' in command
    assert 'export LIVEKIT_URL="${LIVEKIT_AGENT_URL:-ws://localhost:7880}"' in command
    assert 'export LIVEKIT_URL="${LIVEKIT_URL:-ws://localhost:7880}"' in command
    assert "LIVEKIT_DISPATCHER_INSTRUCTIONS_PATH" not in command
    assert (
        "exec /opt/openbase/python/bin/python -m openbase_coder_cli.livekit_agent.livekit start"
        in command
    )
    assert service.workdir_template == "{runtime_workdir}"


def test_django_service_uses_livekit_network_mode_for_room_url():
    service = next(svc for svc in SERVICES if svc.name == "django-cli")
    command = service.command_template.format(
        openbase_coder="/usr/local/bin/openbase-coder",
        data_dir="/tmp/openbase",
        workspace="/tmp/workspace",
    )

    assert 'LIVEKIT_NETWORK_MODE="${LIVEKIT_NETWORK_MODE:-tailscale}"' in command
    assert "Ignoring invalid Tailscale IPv4 value: $LIVEKIT_NODE_IP" in command
    assert (
        "LIVEKIT_NODE_IP is required to derive LIVEKIT_URL in Tailscale mode."
        in command
    )
    assert 'export LIVEKIT_URL="ws://${LIVEKIT_NODE_IP}:7880"' in command
    assert 'export LIVEKIT_URL="${LIVEKIT_URL:-ws://localhost:7880}"' in command


def test_sync_workers_service_is_auto_installed_service():
    service = next(svc for svc in SERVICES if svc.name == "sync-workers")
    command = service.command_template.format(
        openbase_coder="/usr/local/bin/openbase-coder",
        data_dir="/tmp/openbase",
        workspace="/tmp/workspace",
    )

    assert service.workdir_template == "{data_dir}"
    assert service.install_by_default is True
    assert command == "exec /usr/local/bin/openbase-coder sync-workers run"


def test_thread_sync_services_are_retired():
    from openbase_coder_cli.services.definitions import (
        RETIRED_SERVICE_NAMES,
        retired_service_stub,
    )

    defined_names = {svc.name for svc in SERVICES}
    for name in (
        "codex-thread-sync",
        "claude-thread-sync",
        "codex-thread-device-sync",
        "claude-thread-device-sync",
    ):
        assert name in RETIRED_SERVICE_NAMES
        assert name not in defined_names
        stub = retired_service_stub(name)
        assert stub.name == name
        assert stub.install_by_default is False


def test_sync_workers_jobs_cover_the_retired_services_and_reconcile():
    from openbase_coder_cli.cli.sync_workers import build_jobs

    names = {job.name for job in build_jobs()}
    assert names == {
        "codex_thread_sync",
        "claude_thread_sync",
        "codex_thread_device_sync",
        "claude_thread_device_sync",
        "code_sync_reconcile",
    }


def test_sync_workers_intervals_respect_env_overrides(monkeypatch):
    from openbase_coder_cli.cli.sync_workers import build_jobs

    monkeypatch.setenv("CODEX_THREAD_SYNC_INTERVAL", "120")
    monkeypatch.setenv("CODE_SYNC_TICK_SECONDS", "30")
    monkeypatch.setenv("CLAUDE_THREAD_SYNC_INTERVAL", "not-a-number")
    jobs = {job.name: job for job in build_jobs()}
    assert jobs["codex_thread_sync"].interval == 120.0
    assert jobs["code_sync_reconcile"].interval == 30.0
    # Bad values fall back to the default rather than crashing the service.
    assert jobs["claude_thread_sync"].interval == 60.0


def test_openbase_routines_service_is_auto_installed_service():
    service = next(svc for svc in SERVICES if svc.name == "openbase-routines")
    command = service.command_template.format(
        openbase_coder="/usr/local/bin/openbase-coder",
        data_dir="/tmp/openbase",
        workspace="/tmp/workspace",
    )

    assert service.workdir_template == "{data_dir}"
    assert (
        'OPENBASE_CODER_ROUTINES_INTERVAL="${OPENBASE_CODER_ROUTINES_INTERVAL:-60}"'
        in command
    )
    assert (
        'exec /usr/local/bin/openbase-coder routines run-loop --interval "$OPENBASE_CODER_ROUTINES_INTERVAL"'
        in command
    )
