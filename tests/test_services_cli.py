from __future__ import annotations

import importlib
from types import SimpleNamespace

from click.testing import CliRunner

services_cli = importlib.import_module("openbase_coder_cli.cli.services")


def test_services_uninstall_command_is_registered():
    help_result = CliRunner().invoke(services_cli.services, ["--help"])

    assert help_result.exit_code == 0
    assert "uninstall" in help_result.output


def test_services_status_fails_when_tailscale_serve_health_fails(monkeypatch):
    monkeypatch.setattr(services_cli, "require_installation", lambda: None)
    monkeypatch.setattr(services_cli, "configured_coding_backends", lambda: ["codex"])
    monkeypatch.setattr(
        services_cli,
        "SERVICES",
        [
            SimpleNamespace(
                name="django-cli",
                install_by_default=True,
                supports_backend=lambda _backend: True,
            )
        ],
    )
    monkeypatch.setattr(
        services_cli,
        "launchctl_status",
        lambda _svc: {"installed": True, "pid": 1234},
    )
    monkeypatch.setattr(
        services_cli,
        "tailscale_serve_health",
        lambda: SimpleNamespace(
            healthy=False,
            tailscale_available=True,
            tailscale_running=True,
            host="mac.tailnet.ts.net",
            openbase_url="http://mac.tailnet.ts.net:18080",
            openbase_configured=True,
            livekit_configured=True,
            openbase_reachable=False,
            error="connection refused",
        ),
    )

    result = CliRunner().invoke(services_cli.services, ["status"])

    assert result.exit_code != 0
    assert "external-health     failed (connection refused)" in result.output
    assert "One or more Openbase services are unhealthy." in result.output


def test_services_install_configures_tailscale_serve_routes(monkeypatch):
    calls = []

    monkeypatch.setattr(services_cli, "require_installation", lambda: object())
    monkeypatch.setattr(
        services_cli,
        "install_all_services",
        lambda _config: calls.append("install"),
    )
    monkeypatch.setattr(
        services_cli,
        "configure_tailscale_serve",
        lambda: calls.append("tailscale"),
    )

    result = CliRunner().invoke(services_cli.services, ["install"])

    assert result.exit_code == 0, result.output
    assert calls == ["install", "tailscale"]
    assert "Configured :18080 -> http://127.0.0.1:7999" in result.output
    assert "Configured tcp :7880 -> tcp://127.0.0.1:7880" in result.output


def test_services_start_all_configures_tailscale_serve_routes(monkeypatch):
    calls = []
    targets = [
        SimpleNamespace(name="django-cli"),
        SimpleNamespace(name="livekit-server"),
    ]

    monkeypatch.setattr(services_cli, "require_installation", lambda: object())
    monkeypatch.setattr(services_cli, "target_services", lambda _name: targets)
    monkeypatch.setattr(
        services_cli,
        "_ensure_started",
        lambda _config, svc, _verb: calls.append(svc.name),
    )
    monkeypatch.setattr(
        services_cli,
        "configure_tailscale_serve",
        lambda: calls.append("tailscale"),
    )

    result = CliRunner().invoke(services_cli.services, ["start"])

    assert result.exit_code == 0, result.output
    assert calls == ["django-cli", "livekit-server", "tailscale"]


def test_services_start_one_does_not_configure_tailscale_serve_routes(monkeypatch):
    calls = []
    target = SimpleNamespace(name="django-cli")

    monkeypatch.setattr(services_cli, "require_installation", lambda: object())
    monkeypatch.setattr(services_cli, "target_services", lambda _name: [target])
    monkeypatch.setattr(
        services_cli,
        "_ensure_started",
        lambda _config, svc, _verb: calls.append(svc.name),
    )
    monkeypatch.setattr(
        services_cli,
        "configure_tailscale_serve",
        lambda: calls.append("tailscale"),
    )

    result = CliRunner().invoke(services_cli.services, ["start", "django-cli"])

    assert result.exit_code == 0, result.output
    assert calls == ["django-cli"]


def test_services_status_allows_optional_stopped_service(monkeypatch):
    monkeypatch.setattr(services_cli, "require_installation", lambda: None)
    monkeypatch.setattr(
        services_cli,
        "SERVICES",
        [SimpleNamespace(name="codex-thread-device-sync", install_by_default=False)],
    )
    monkeypatch.setattr(
        services_cli,
        "launchctl_status",
        lambda _svc: {"installed": True, "pid": None, "last_exit_code": None},
    )
    monkeypatch.setattr(
        services_cli,
        "tailscale_serve_health",
        lambda: SimpleNamespace(
            healthy=True,
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

    result = CliRunner().invoke(services_cli.services, ["status"])

    assert result.exit_code == 0, result.output
    assert "codex-thread-device-sync optional (not running, last exit: None)" in (
        result.output
    )


def test_services_status_handles_uninstalled_codex_app_server(monkeypatch):
    monkeypatch.setattr(services_cli, "require_installation", lambda: None)
    monkeypatch.setattr(services_cli, "configured_coding_backends", lambda: ["codex"])
    monkeypatch.setattr(
        services_cli,
        "SERVICES",
        [SimpleNamespace(name="codex-app-server", install_by_default=True)],
    )
    monkeypatch.setattr(
        services_cli, "launchctl_status", lambda _svc: {"installed": False}
    )
    monkeypatch.setattr(
        "openbase_coder_cli.codex_control_plane.shared_codex_daemon_ready",
        lambda: False,
    )

    result = CliRunner().invoke(services_cli.services, ["status"])

    assert result.exit_code != 0
    assert "codex-app-server     not installed" in result.output
    assert not isinstance(result.exception, KeyError)


def test_uninstall_sweeps_all_services_without_installation(monkeypatch):
    removed = []
    monkeypatch.setattr(
        services_cli, "remove_service", lambda svc: removed.append(svc.name) or True
    )
    monkeypatch.setattr(
        services_cli, "any_service_action_interrupts_voice", lambda *_: False
    )

    result = CliRunner().invoke(services_cli.services, ["uninstall"])

    assert result.exit_code == 0
    assert set(removed) == {svc.name for svc in services_cli.SERVICES}


def _healthy_serve():
    return SimpleNamespace(
        healthy=True,
        tailscale_available=True,
        tailscale_running=True,
        host="mac.tailnet.ts.net",
        openbase_url="http://mac.tailnet.ts.net:18080",
        openbase_configured=True,
        livekit_configured=True,
        openbase_reachable=True,
        error=None,
    )


def _skew(service, running, installed, shared_daemon=False):
    from openbase_coder_cli.services.codex_version_skew import CodexVersionSkew

    return CodexVersionSkew(
        service=service,
        running_version=running,
        installed_version=installed,
        installed_path="/opt/codex",
        shared_daemon=shared_daemon,
    )


def test_services_status_reports_shared_daemon_even_with_idle_runner_pid(monkeypatch):
    monkeypatch.setattr(services_cli, "require_installation", lambda: None)
    monkeypatch.setattr(services_cli, "configured_coding_backends", lambda: ["codex"])
    monkeypatch.setattr(
        services_cli,
        "SERVICES",
        [SimpleNamespace(name="codex-app-server", install_by_default=True)],
    )
    # The Openbase runner idles behind the daemon, so launchd reports a pid.
    monkeypatch.setattr(
        services_cli,
        "launchctl_status",
        lambda _svc: {"installed": True, "pid": 4242, "last_exit_code": 0},
    )
    monkeypatch.setattr(
        "openbase_coder_cli.codex_control_plane.shared_codex_daemon_ready",
        lambda: True,
    )
    monkeypatch.setattr(
        services_cli,
        "service_version_skew",
        lambda name: _skew(name, "0.161.0", "0.160.1", shared_daemon=True),
    )
    monkeypatch.setattr(services_cli, "tailscale_serve_health", _healthy_serve)

    result = CliRunner().invoke(services_cli.services, ["status"])

    assert result.exit_code == 0, result.output
    assert "codex-app-server     available through the shared Codex daemon (0.161.0)" in (
        result.output
    )
    assert "upgrade the Codex CLI to 0.161.0" in result.output
    assert "pid 4242" not in result.output


def test_services_status_newer_own_server_asks_for_cli_upgrade_not_restart(monkeypatch):
    monkeypatch.setattr(services_cli, "require_installation", lambda: None)
    monkeypatch.setattr(services_cli, "configured_coding_backends", lambda: ["codex"])
    monkeypatch.setattr(
        services_cli,
        "SERVICES",
        [SimpleNamespace(name="codex-app-server-dispatcher", install_by_default=True)],
    )
    monkeypatch.setattr(
        services_cli,
        "launchctl_status",
        lambda _svc: {"installed": True, "pid": 77, "last_exit_code": 0},
    )
    monkeypatch.setattr(services_cli, "codex_app_server_ready", lambda endpoint: True)
    monkeypatch.setattr(
        "openbase_coder_cli.codex_control_plane.dispatcher_codex_app_server_endpoint",
        lambda: "dispatcher-endpoint",
    )
    monkeypatch.setattr(
        services_cli,
        "service_version_skew",
        lambda name: _skew(name, "0.161.0", "0.160.1"),
    )
    monkeypatch.setattr(services_cli, "tailscale_serve_health", _healthy_serve)

    result = CliRunner().invoke(services_cli.services, ["status"])

    assert result.exit_code == 0, result.output
    assert "Codex 0.161.0 but 0.160.1 is installed — upgrade the Codex CLI to 0.161.0" in (
        result.output
    )
    assert "restart to update" not in result.output
