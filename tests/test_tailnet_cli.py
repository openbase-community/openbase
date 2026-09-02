from __future__ import annotations

import importlib
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

# The cli package re-exports the click Group as `tailnet`, shadowing the
# submodule attribute; load the module object explicitly.
tailnet_cli = importlib.import_module("openbase_coder_cli.cli.tailnet")
from openbase_coder_cli.env_file import env_file_values  # noqa: E402
from openbase_coder_cli.services import cloud_registration  # noqa: E402


@pytest.fixture
def env_path(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("OPENBASE_CODER_CLI_ALLOWED_HOSTS=localhost,127.0.0.1,.ts.net\n")
    monkeypatch.setattr(tailnet_cli, "_env_path", lambda: path)
    return path


@pytest.fixture
def quiet_orchestration(monkeypatch):
    """Stub every side-effectful step so tests only exercise the decisions."""
    calls: dict[str, list] = {
        "teardown": [],
        "bring_up": [],
        "restart": 0,
        "reregister": 0,
        "legacy_bootout": 0,
        "push": [],
    }
    monkeypatch.setattr(
        tailnet_cli, "_teardown_transport", lambda prev: calls["teardown"].append(prev)
    )
    monkeypatch.setattr(
        tailnet_cli, "_bring_up_transport", lambda name: calls["bring_up"].append(name)
    )
    monkeypatch.setattr(
        tailnet_cli,
        "_restart_transport_services",
        lambda: calls.__setitem__("restart", calls["restart"] + 1),
    )
    monkeypatch.setattr(
        tailnet_cli,
        "_reregister_device",
        lambda: calls.__setitem__("reregister", calls["reregister"] + 1),
    )
    monkeypatch.setattr(
        tailnet_cli,
        "_bootout_legacy_tunneld_agent",
        lambda: calls.__setitem__("legacy_bootout", calls["legacy_bootout"] + 1),
    )

    def fake_push(provider):
        calls["push"].append(provider)
        return cloud_registration.CloudReportResult(ok=True, supported=True)

    monkeypatch.setattr(cloud_registration, "push_tailnet_provider", fake_push)
    return calls


def test_set_provider_writes_env_and_orchestrates(env_path, quiet_orchestration):
    result = CliRunner().invoke(tailnet_cli.tailnet, ["set-provider", "netmesh-tsnet"])
    assert result.exit_code == 0, result.output

    values = env_file_values(Path(env_path))
    assert values["OPENBASE_CODER_CLI_TAILSCALE_PROVIDER"] == "netmesh-tsnet"
    assert ".netmesh.openbase.cloud" in values["OPENBASE_CODER_CLI_ALLOWED_HOSTS"]
    assert values["LIVEKIT_NETWORK_MODE"] == "local"

    assert quiet_orchestration["push"] == ["netmesh-tsnet"]
    assert quiet_orchestration["legacy_bootout"] == 1
    assert quiet_orchestration["bring_up"] == ["netmesh-tsnet"]
    assert quiet_orchestration["teardown"] == ["tailscale"]
    assert quiet_orchestration["restart"] == 1
    assert quiet_orchestration["reregister"] == 1


def test_set_provider_back_to_tailscale_restores_livekit_mode(
    env_path, quiet_orchestration
):
    runner = CliRunner()
    runner.invoke(tailnet_cli.tailnet, ["set-provider", "netmesh-tsnet"])
    result = runner.invoke(tailnet_cli.tailnet, ["set-provider", "tailscale"])
    assert result.exit_code == 0, result.output

    values = env_file_values(Path(env_path))
    assert values["OPENBASE_CODER_CLI_TAILSCALE_PROVIDER"] == "tailscale"
    assert values["LIVEKIT_NETWORK_MODE"] == "tailscale"
    assert quiet_orchestration["teardown"][-1] == "netmesh-tsnet"


def test_set_provider_no_cloud_skips_push(env_path, quiet_orchestration):
    result = CliRunner().invoke(
        tailnet_cli.tailnet, ["set-provider", "netmesh", "--no-cloud"]
    )
    assert result.exit_code == 0, result.output
    assert quiet_orchestration["push"] == []


def test_set_provider_same_value_skips_teardown_but_cleans_legacy(
    env_path, quiet_orchestration
):
    result = CliRunner().invoke(tailnet_cli.tailnet, ["set-provider", "tailscale"])
    assert result.exit_code == 0, result.output
    assert quiet_orchestration["teardown"] == []
    assert quiet_orchestration["legacy_bootout"] == 1


def test_sync_applies_cloud_value_without_pushing(
    env_path, quiet_orchestration, monkeypatch
):
    monkeypatch.setattr(
        cloud_registration, "fetch_tailnet_provider", lambda: "netmesh-tsnet"
    )
    result = CliRunner().invoke(tailnet_cli.tailnet, ["sync", "--apply"])
    assert result.exit_code == 0, result.output

    values = env_file_values(Path(env_path))
    assert values["OPENBASE_CODER_CLI_TAILSCALE_PROVIDER"] == "netmesh-tsnet"
    assert quiet_orchestration["push"] == []


def test_sync_reports_drift_without_apply(env_path, quiet_orchestration, monkeypatch):
    monkeypatch.setattr(cloud_registration, "fetch_tailnet_provider", lambda: "netmesh")
    result = CliRunner().invoke(tailnet_cli.tailnet, ["sync"])
    assert result.exit_code == 0, result.output
    assert "netmesh" in result.output
    values = env_file_values(Path(env_path))
    assert "OPENBASE_CODER_CLI_TAILSCALE_PROVIDER" not in values


def test_enroll_without_login_fails_cleanly(env_path, monkeypatch):
    monkeypatch.setattr(cloud_registration, "netmesh_enroll", lambda: None)
    result = CliRunner().invoke(tailnet_cli.tailnet, ["enroll"])
    assert result.exit_code != 0
    assert "login" in result.output


def test_netmesh_routes_through_stock_tailscale_off_macos(monkeypatch):
    tp = importlib.import_module("openbase_coder_cli.services.tailscale_provider")
    monkeypatch.setenv("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER", "netmesh")

    monkeypatch.setattr("platform.system", lambda: "Windows")
    assert tp.netmesh_uses_stock_tailscale() is True
    monkeypatch.setattr(tp, "tailscale_bin", lambda: "C:\\ts\\tailscale.exe")
    assert tp.tool_path() == "C:\\ts\\tailscale.exe"

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    assert tp.netmesh_uses_stock_tailscale() is False
    monkeypatch.setattr(tp, "netmesh_ctl_bin", lambda: "/n/netmesh-ctl")
    assert tp.tool_path() == "/n/netmesh-ctl"


def test_bring_up_embedded_transport_installs_binary_before_service(monkeypatch):
    from openbase_coder_cli.services import launchd, tunneld
    from openbase_coder_cli.services.installation import InstallationConfig

    config = InstallationConfig(workspace_path="workspace")
    calls = []
    monkeypatch.setattr(InstallationConfig, "load", lambda: config)
    monkeypatch.setattr(
        tunneld,
        "install_tunneld_binary",
        lambda received: calls.append(("binary", received)),
    )
    monkeypatch.setattr(
        launchd,
        "install_service",
        lambda received, service: calls.append(("service", received, service.name)),
    )
    monkeypatch.setattr(
        launchd,
        "launchctl_kickstart",
        lambda service: calls.append(("kickstart", service.name)),
    )
    monkeypatch.setattr(
        tunneld,
        "ensure_tunneld_running",
        lambda **kwargs: calls.append(("running", kwargs)),
    )

    tailnet_cli._bring_up_transport("netmesh-tsnet")

    assert calls == [
        ("binary", config),
        ("service", config, "openbase-tunneld"),
        ("kickstart", "openbase-tunneld"),
        ("running", {"managed_service": True}),
    ]


def test_bring_up_embedded_transport_fails_when_binary_install_fails(monkeypatch):
    from openbase_coder_cli.services import tunneld
    from openbase_coder_cli.services.installation import InstallationConfig

    monkeypatch.setattr(
        InstallationConfig,
        "load",
        lambda: InstallationConfig(workspace_path="workspace"),
    )

    def fail_install(_config):
        raise RuntimeError("Go toolchain unavailable")

    monkeypatch.setattr(tunneld, "install_tunneld_binary", fail_install)

    with pytest.raises(
        click.ClickException, match="Could not install openbase-tunneld"
    ):
        tailnet_cli._bring_up_transport("netmesh-tsnet")


def test_tailnet_status_renders_state_and_peer_paths(monkeypatch):
    tp = importlib.import_module("openbase_coder_cli.services.tailscale_provider")
    monkeypatch.setattr(tp, "provider", lambda: "netmesh-tsnet")
    monkeypatch.setattr(
        tp,
        "status_json",
        lambda: {
            "BackendState": "Running",
            "Self": {
                "DNSName": "mac.netmesh.openbase.cloud.",
                "TailscaleIPs": ["100.64.0.10"],
            },
            "Peer": {
                "a": {
                    "HostName": "iphone",
                    "TailscaleIPs": ["100.64.0.11"],
                    "Online": True,
                    "CurAddr": "192.168.0.59:41641",
                },
                "b": {
                    "HostName": "testpeer",
                    "TailscaleIPs": ["100.64.0.2"],
                    "Online": False,
                },
            },
        },
    )

    result = CliRunner().invoke(tailnet_cli.tailnet, ["status"])

    assert result.exit_code == 0, result.output
    assert "state:" in result.output and "Running" in result.output
    assert "mac.netmesh.openbase.cloud" in result.output
    assert "direct 192.168.0.59:41641" in result.output
    assert "offline" in result.output


def test_tailnet_status_errors_when_provider_unreachable(monkeypatch):
    tp = importlib.import_module("openbase_coder_cli.services.tailscale_provider")
    monkeypatch.setattr(tp, "status_json", lambda: {"error": "daemon not running"})

    result = CliRunner().invoke(tailnet_cli.tailnet, ["status"])

    assert result.exit_code != 0
    assert "daemon not running" in result.output


def test_provider_reads_env_file_as_single_source_of_truth(
    monkeypatch, _isolated_host_state
):
    tp = importlib.import_module("openbase_coder_cli.services.tailscale_provider")
    env_path = _isolated_host_state
    env_path.write_text("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER=netmesh\n")

    monkeypatch.delenv("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER", raising=False)
    assert tp.provider() == "netmesh"

    # The installed file wins even over a stale shell export.
    monkeypatch.setenv("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER", "tailscale")
    assert tp.provider() == "netmesh"

    # No file value -> the env var decides; nothing anywhere -> default.
    env_path.write_text("")
    assert tp.provider() == "tailscale"
    monkeypatch.setenv("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER", "netmesh-tsnet")
    assert tp.provider() == "netmesh-tsnet"
    monkeypatch.delenv("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER", raising=False)
    assert tp.provider() == "tailscale"
