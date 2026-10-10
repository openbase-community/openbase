"""Fresh Openbase VPN installs: LiveKit waits for pairing, then switches over."""

from __future__ import annotations

import pytest

from openbase_coder_cli.services import runners, tailnet_transition


class FakeNetwork:
    """The VPN's address: absent until pairing enrolls the node."""

    def __init__(self) -> None:
        self.ipv4: str | None = None

    def tailscale_ip(self, family: str = "4") -> str | None:
        return self.ipv4 if family == "4" else None

    def resolve_interface(self, ip: str) -> str | None:
        return "utun4" if ip == self.ipv4 else None


@pytest.fixture
def vpn(monkeypatch, tmp_path):
    from openbase_coder_cli.services import launchd
    from openbase_coder_cli.services.freshness import runtime as freshness_runtime

    network = FakeNetwork()
    monkeypatch.setattr(runners.network, "tailscale_ip", network.tailscale_ip)
    monkeypatch.setattr(runners.network, "resolve_interface", network.resolve_interface)
    monkeypatch.setattr(
        tailnet_transition, "AWAITING_TAILNET_MARKER", tmp_path / "awaiting"
    )
    monkeypatch.setattr(tailnet_transition, "_last_restart_monotonic", None)
    monkeypatch.setattr(
        runners.InstallationConfig, "exists", staticmethod(lambda: False)
    )
    monkeypatch.setattr(
        runners,
        "load_service_env",
        lambda config: {"LIVEKIT_NETWORK_MODE": "tailscale"},
    )
    monkeypatch.setattr(
        runners, "_resolve_binaries", lambda name, config: {"livekit": "livekit-server"}
    )
    monkeypatch.setattr(launchd, "cap_service_log", lambda name: None)
    monkeypatch.setattr(
        freshness_runtime, "capture_service", lambda name, config, argv: None
    )
    monkeypatch.setattr(tailnet_transition, "_livekit_server_running", lambda: True)
    monkeypatch.setattr(
        "openbase_coder_cli.services.livekit_pool_watchdog._voice_session_active",
        lambda: False,
    )
    network.execs = []
    monkeypatch.setattr(
        runners.os, "execvpe", lambda file, argv, env: network.execs.append(argv)
    )
    network.restarts = []
    monkeypatch.setattr(
        tailnet_transition,
        "_restart_transport_services",
        lambda: network.restarts.append(tailnet_transition.TRANSPORT_SERVICES),
    )
    return network


def _node_ip(argv: list[str]) -> str:
    return argv[argv.index("--node-ip") + 1]


def test_livekit_defers_until_pairing_then_restarts_with_vpn_address(vpn):
    # Setup: no VPN address yet. LiveKit starts loopback-only (so setup's
    # readiness wait passes) and records that it is waiting for the tailnet.
    runners.run("livekit-server")
    assert _node_ip(vpn.execs[-1]) == "127.0.0.1"
    assert tailnet_transition.AWAITING_TAILNET_MARKER.exists()

    # Before pairing the watcher leaves the services alone.
    assert tailnet_transition.run_tick() is False
    assert vpn.restarts == []

    # Pairing enrolls the node; the next tick restarts the transport services.
    vpn.ipv4 = "100.64.1.2"
    assert tailnet_transition.run_tick() is True
    assert vpn.restarts == [("livekit-server", "livekit-agent", "django-cli")]

    # The restarted server advertises the VPN address and clears the marker,
    # so later ticks do nothing.
    runners.run("livekit-server")
    assert _node_ip(vpn.execs[-1]) == "100.64.1.2"
    assert not tailnet_transition.AWAITING_TAILNET_MARKER.exists()
    assert tailnet_transition.run_tick() is False
    assert len(vpn.restarts) == 1


def test_tick_defers_during_a_voice_call(vpn, monkeypatch):
    runners.run("livekit-server")
    vpn.ipv4 = "100.64.1.2"
    monkeypatch.setattr(
        "openbase_coder_cli.services.livekit_pool_watchdog._voice_session_active",
        lambda: True,
    )
    assert tailnet_transition.run_tick() is False
    assert vpn.restarts == []


def test_tick_ignores_marker_when_livekit_server_is_not_running(vpn, monkeypatch):
    runners.run("livekit-server")
    vpn.ipv4 = "100.64.1.2"
    monkeypatch.setattr(tailnet_transition, "_livekit_server_running", lambda: False)
    assert tailnet_transition.run_tick() is False
    assert vpn.restarts == []


def test_tick_retries_no_sooner_than_retry_window(vpn, monkeypatch):
    runners.run("livekit-server")
    vpn.ipv4 = "100.64.1.2"
    clock = iter([1000.0, 1000.0 + 60, 1000.0 + tailnet_transition.RETRY_SECONDS])
    monkeypatch.setattr(tailnet_transition.time, "monotonic", lambda: next(clock))
    # The marker survives (the restart raced the address away again).
    assert tailnet_transition.run_tick() is True
    assert tailnet_transition.run_tick() is False
    assert tailnet_transition.run_tick() is True
    assert len(vpn.restarts) == 2


def test_non_tailscale_mode_never_restarts(vpn, monkeypatch):
    tailnet_transition.record_livekit_start(awaiting_tailnet=True)
    vpn.ipv4 = "100.64.1.2"
    monkeypatch.setattr(
        runners, "load_service_env", lambda config: {"LIVEKIT_NETWORK_MODE": "local"}
    )
    assert tailnet_transition.run_tick() is False
    assert vpn.restarts == []
