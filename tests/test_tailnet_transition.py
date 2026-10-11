"""Fresh Openbase VPN installs: LiveKit waits for pairing, then switches over."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

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
    monkeypatch.setattr(tailnet_transition, "LEASE_PATH", tmp_path / "lease")
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
    assert not tailnet_transition.AWAITING_TAILNET_MARKER.exists()

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
    clock = [1000.0]
    monkeypatch.setattr(tailnet_transition.time, "monotonic", lambda: clock[0])

    def restart_without_address():
        vpn.restarts.append(tailnet_transition.TRANSPORT_SERVICES)
        vpn.ipv4 = None
        runners.run("livekit-server")
        vpn.ipv4 = "100.64.1.2"

    monkeypatch.setattr(
        tailnet_transition, "_restart_transport_services", restart_without_address
    )
    # The marker survives (the restart raced the address away again).
    assert tailnet_transition.run_tick() is True
    clock[0] += 60
    assert tailnet_transition.run_tick() is False
    clock[0] = 1000.0 + tailnet_transition.RETRY_SECONDS
    assert tailnet_transition.run_tick() is True
    assert len(vpn.restarts) == 2


def test_partial_transition_retries_after_server_has_recovered(vpn, monkeypatch):
    runners.run("livekit-server")
    vpn.ipv4 = "100.64.1.2"
    clock = [1000.0]
    monkeypatch.setattr(tailnet_transition.time, "monotonic", lambda: clock[0])

    def restart_with_consumer_failure():
        runners.run("livekit-server")
        raise RuntimeError("agent restart failed")

    monkeypatch.setattr(
        tailnet_transition, "_restart_transport_services", restart_with_consumer_failure
    )
    with pytest.raises(RuntimeError, match="agent restart failed"):
        tailnet_transition.run_tick()
    assert _node_ip(vpn.execs[-1]) == "100.64.1.2"
    assert tailnet_transition.AWAITING_TAILNET_MARKER.exists()

    monkeypatch.setattr(tailnet_transition, "_restart_transport_services", lambda: None)
    clock[0] += tailnet_transition.RETRY_SECONDS
    assert tailnet_transition.run_tick() is True
    assert not tailnet_transition.AWAITING_TAILNET_MARKER.exists()


def test_independent_server_restart_keeps_consumer_transition_pending(vpn):
    runners.run("livekit-server")
    vpn.ipv4 = "100.64.1.2"
    runners.run("livekit-server")
    assert _node_ip(vpn.execs[-1]) == "100.64.1.2"
    assert tailnet_transition.AWAITING_TAILNET_MARKER.exists()
    assert tailnet_transition.run_tick() is True
    assert vpn.restarts == [tailnet_transition.TRANSPORT_SERVICES]
    assert not tailnet_transition.AWAITING_TAILNET_MARKER.exists()


def test_non_tailscale_mode_never_restarts(vpn, monkeypatch):
    tailnet_transition.record_livekit_start(awaiting_tailnet=True)
    vpn.ipv4 = "100.64.1.2"
    monkeypatch.setattr(
        runners, "load_service_env", lambda config: {"LIVEKIT_NETWORK_MODE": "local"}
    )
    assert tailnet_transition.run_tick() is False
    assert vpn.restarts == []


class LoginKicks:
    """Login's in-place kicks, with the livekit-server runner behind its kick."""

    def __init__(self) -> None:
        self.kicked: list[str] = []
        self.failing: set[str] = set()

    def kickstart(self, service) -> bool:
        self.kicked.append(service.name)
        if service.name in self.failing:
            return False
        if service.name == "livekit-server":
            runners.run("livekit-server")
        return True


@pytest.fixture
def login_kickstart(vpn, monkeypatch):
    from openbase_coder_cli.services import launchd

    kicks = LoginKicks()
    monkeypatch.setattr(launchd, "launchctl_kickstart", kicks.kickstart)
    return kicks


def test_login_restart_with_address_settles_transition(vpn, login_kickstart):
    # Setup left LiveKit loopback-only; sign-in connects the VPN and kicks
    # the transport services itself.
    runners.run("livekit-server")
    vpn.ipv4 = "100.64.1.2"

    assert tailnet_transition.kickstart_transport_services() is True

    assert login_kickstart.kicked == ["livekit-server", "livekit-agent", "django-cli"]
    assert _node_ip(vpn.execs[-1]) == "100.64.1.2"
    assert not tailnet_transition.AWAITING_TAILNET_MARKER.exists()
    # The job must not restart everything a second time as pairing begins.
    assert tailnet_transition.run_tick() is False
    assert vpn.restarts == []


def test_login_restart_without_address_keeps_transition_pending(vpn, login_kickstart):
    runners.run("livekit-server")

    assert tailnet_transition.kickstart_transport_services() is True

    assert _node_ip(vpn.execs[-1]) == "127.0.0.1"
    assert tailnet_transition.AWAITING_TAILNET_MARKER.exists()
    vpn.ipv4 = "100.64.1.2"
    assert tailnet_transition.run_tick() is True
    assert vpn.restarts == [tailnet_transition.TRANSPORT_SERVICES]


def test_login_restart_that_fails_leaves_transition_to_the_job(vpn, login_kickstart):
    runners.run("livekit-server")
    vpn.ipv4 = "100.64.1.2"
    login_kickstart.failing.add("django-cli")

    assert tailnet_transition.kickstart_transport_services() is False

    assert tailnet_transition.AWAITING_TAILNET_MARKER.exists()
    assert tailnet_transition.run_tick() is True
    assert vpn.restarts == [tailnet_transition.TRANSPORT_SERVICES]
    assert not tailnet_transition.AWAITING_TAILNET_MARKER.exists()


def test_tailnet_cli_restart_delegates_to_the_transition_kick(monkeypatch):
    import importlib

    tailnet_cli = importlib.import_module("openbase_coder_cli.cli.tailnet")

    calls: list[str] = []
    monkeypatch.setattr(
        tailnet_transition,
        "kickstart_transport_services",
        lambda: calls.append("kick") or True,
    )
    tailnet_cli._restart_transport_services()
    assert calls == ["kick"]


def test_job_stands_back_while_login_holds_the_transport_lease(
    vpn, login_kickstart, monkeypatch, tmp_path
):
    monkeypatch.setattr(tailnet_transition, "LEASE_PATH", tmp_path / "lease")
    runners.run("livekit-server")

    with tailnet_transition.transport_lease():
        # Login connected the VPN but has not kicked the services yet (serve
        # rules in between): the job must not start its own restart batch.
        vpn.ipv4 = "100.64.1.2"
        assert tailnet_transition.run_tick() is False
        assert tailnet_transition.kickstart_transport_services() is True

    assert not tailnet_transition.LEASE_PATH.exists()
    assert vpn.restarts == []
    assert tailnet_transition.run_tick() is False


def test_stale_lease_from_a_dead_login_expires(vpn, monkeypatch, tmp_path):
    monkeypatch.setattr(tailnet_transition, "LEASE_PATH", tmp_path / "lease")
    runners.run("livekit-server")
    vpn.ipv4 = "100.64.1.2"
    tailnet_transition.LEASE_PATH.touch()
    assert tailnet_transition.run_tick() is False

    monkeypatch.setattr(
        tailnet_transition.time,
        "time",
        lambda: (
            tailnet_transition.LEASE_PATH.stat().st_mtime
            + tailnet_transition.LEASE_SECONDS
        ),
    )
    assert tailnet_transition.run_tick() is True
    assert vpn.restarts == [tailnet_transition.TRANSPORT_SERVICES]


def test_job_rechecks_lease_after_network_probe(vpn, monkeypatch, tmp_path):
    monkeypatch.setattr(tailnet_transition, "LEASE_PATH", tmp_path / "lease")
    runners.run("livekit-server")

    def login_connects_during_probe():
        tailnet_transition.LEASE_PATH.touch()
        vpn.ipv4 = "100.64.1.2"
        return True

    monkeypatch.setattr(
        tailnet_transition, "_tailnet_ready", login_connects_during_probe
    )
    assert tailnet_transition.run_tick() is False
    assert vpn.restarts == []


def test_job_rechecks_pending_transition_after_network_probe(
    vpn, login_kickstart, monkeypatch
):
    runners.run("livekit-server")

    def login_finishes_during_probe():
        vpn.ipv4 = "100.64.1.2"
        assert tailnet_transition.kickstart_transport_services() is True
        return True

    monkeypatch.setattr(
        tailnet_transition, "_tailnet_ready", login_finishes_during_probe
    )
    assert tailnet_transition.run_tick() is False
    assert vpn.restarts == []


def test_loopback_start_during_marker_removal_is_not_lost(vpn, monkeypatch):
    runners.run("livekit-server")
    generation = tailnet_transition.pending_generation()
    removal_started = Event()
    recorded = Event()
    original_unlink = Path.unlink

    def record_during_removal():
        assert removal_started.wait(timeout=2)
        tailnet_transition.record_livekit_start(awaiting_tailnet=True)
        recorded.set()

    def unlink(path, *args, **kwargs):
        if path == tailnet_transition.AWAITING_TAILNET_MARKER:
            removal_started.set()
            recorded.wait(timeout=0.1)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(record_during_removal)
        tailnet_transition.finish_transition(generation)
        writer.result(timeout=2)

    assert tailnet_transition.AWAITING_TAILNET_MARKER.exists()


def test_async_loopback_start_after_login_kicks_restores_pending_transition(
    vpn, monkeypatch
):
    from openbase_coder_cli.services import launchd

    runners.run("livekit-server")
    monkeypatch.setattr(launchd, "launchctl_kickstart", lambda service: True)

    assert tailnet_transition.kickstart_transport_services() is True
    assert not tailnet_transition.AWAITING_TAILNET_MARKER.exists()

    runners.run("livekit-server")
    assert tailnet_transition.AWAITING_TAILNET_MARKER.exists()
    vpn.ipv4 = "100.64.1.2"
    assert tailnet_transition.run_tick() is True


def test_job_defers_when_another_service_mutation_is_running(vpn):
    runners.run("livekit-server")
    vpn.ipv4 = "100.64.1.2"

    with tailnet_transition.service_mutation():
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert (
                executor.submit(tailnet_transition.run_tick).result(timeout=2) is False
            )

    assert vpn.restarts == []
    assert tailnet_transition.run_tick() is True


def test_restart_batch_serializes_lease_creation(vpn, monkeypatch):
    runners.run("livekit-server")
    vpn.ipv4 = "100.64.1.2"
    attempted = Event()
    entered = Event()

    def login():
        attempted.set()
        with tailnet_transition.transport_lease():
            entered.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        logins = []

        def restart():
            logins.append(executor.submit(login))
            assert attempted.wait(timeout=2)
            assert not entered.wait(timeout=0.1)

        monkeypatch.setattr(tailnet_transition, "_restart_transport_services", restart)
        assert tailnet_transition.run_tick() is True
        logins[0].result(timeout=2)

    assert entered.is_set()


def test_lease_cleanup_preserves_a_newer_owner(vpn):
    with tailnet_transition.transport_lease():
        timestamp = tailnet_transition.LEASE_PATH.stat().st_mtime_ns + 1_000_000
        os.utime(tailnet_transition.LEASE_PATH, ns=(timestamp, timestamp))

    assert tailnet_transition.LEASE_PATH.exists()
