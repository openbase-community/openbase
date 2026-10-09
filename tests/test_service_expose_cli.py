from __future__ import annotations

import importlib

from click.testing import CliRunner

from openbase_coder_cli.services.tunneld import TunneldForwardError

# `openbase_coder_cli.cli` re-exports the `service` click group under the
# module's name, so import the module itself explicitly.
service_cli = importlib.import_module("openbase_coder_cli.cli.service")


def _tsnet(monkeypatch, enabled: bool = True) -> None:
    monkeypatch.setattr(
        service_cli.tailscale_provider, "is_netmesh_tsnet", lambda: enabled
    )


def test_expose_requires_embedded_node(monkeypatch):
    _tsnet(monkeypatch, enabled=False)
    result = CliRunner().invoke(service_cli.service, ["expose", "3000"])
    assert result.exit_code != 0
    assert "service publish" in result.output


def test_expose_creates_forward_and_prints_url(monkeypatch):
    _tsnet(monkeypatch)
    calls = []
    monkeypatch.setattr(service_cli, "local_service_available", lambda port: True)
    monkeypatch.setattr(
        service_cli,
        "tunneld_add_forward",
        lambda port, **kwargs: (
            calls.append((port, kwargs))
            or {"port": port, "expires_at": "2026-10-09T23:00:00Z"}
        ),
    )
    monkeypatch.setattr(
        service_cli, "tunneld_self_dns_name", lambda: "devspace-1.net.obs.so"
    )

    result = CliRunner().invoke(
        service_cli.service,
        ["expose", "1455", "--one-shot", "--ttl", "120", "--peer", "100.64.0.9"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (1455, {"ttl_seconds": 120, "one_shot": True, "peer": "100.64.0.9"})
    ]
    assert "http://devspace-1.net.obs.so:1455/" in result.output
    assert "Closes after the first completed connection" in result.output
    assert "Pinned to peer: 100.64.0.9" in result.output
    assert "2026-10-09T23:00:00Z" in result.output


def test_expose_refuses_idle_port_unless_no_check(monkeypatch):
    _tsnet(monkeypatch)
    monkeypatch.setattr(service_cli, "local_service_available", lambda port: False)
    added = []
    monkeypatch.setattr(
        service_cli,
        "tunneld_add_forward",
        lambda port, **kwargs: added.append(port) or {"port": port},
    )
    monkeypatch.setattr(service_cli, "tunneld_self_dns_name", lambda: None)

    refused = CliRunner().invoke(service_cli.service, ["expose", "3000"])
    assert refused.exit_code != 0
    assert "No service is accepting connections" in refused.output
    assert added == []

    allowed = CliRunner().invoke(service_cli.service, ["expose", "3000", "--no-check"])
    assert allowed.exit_code == 0, allowed.output
    assert added == [3000]
    assert "http://<this-node>:3000/" in allowed.output


def test_expose_reports_daemon_refusal(monkeypatch):
    _tsnet(monkeypatch)
    monkeypatch.setattr(service_cli, "local_service_available", lambda port: True)

    def refuse(port, **kwargs):
        raise TunneldForwardError("port 18080 is reserved by a fixed forward")

    monkeypatch.setattr(service_cli, "tunneld_add_forward", refuse)
    result = CliRunner().invoke(service_cli.service, ["expose", "18080"])
    assert result.exit_code != 0
    assert "reserved by a fixed forward" in result.output


def test_expose_rejects_ttl_over_an_hour(monkeypatch):
    _tsnet(monkeypatch)
    result = CliRunner().invoke(
        service_cli.service, ["expose", "3000", "--ttl", "7200"]
    )
    assert result.exit_code != 0


def test_unexpose(monkeypatch):
    _tsnet(monkeypatch)
    removed = []
    monkeypatch.setattr(
        service_cli,
        "tunneld_remove_forward",
        lambda port: removed.append(port) or port == 3000,
    )
    ok = CliRunner().invoke(service_cli.service, ["unexpose", "3000"])
    assert ok.exit_code == 0, ok.output
    assert "Closed the forward" in ok.output
    missing = CliRunner().invoke(service_cli.service, ["unexpose", "3001"])
    assert missing.exit_code != 0
    assert "not exposed" in missing.output
    assert removed == [3000, 3001]


def test_list_shows_exposed_ports(monkeypatch):
    _tsnet(monkeypatch)
    monkeypatch.setattr(
        service_cli, "load_registry", lambda: service_cli.ServiceRegistry((), None)
    )
    monkeypatch.setattr(
        service_cli,
        "tunneld_list_forwards",
        lambda: [
            {
                "port": 1455,
                "one_shot": True,
                "peer": "100.64.0.9",
                "expires_at": "2026-10-09T23:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        service_cli, "tunneld_self_dns_name", lambda: "devspace-1.net.obs.so"
    )
    result = CliRunner().invoke(service_cli.service, ["list"])
    assert result.exit_code == 0, result.output
    assert "http://devspace-1.net.obs.so:1455/" in result.output
    assert "one-shot" in result.output
    assert "peer 100.64.0.9" in result.output


def test_list_without_forwards_or_services(monkeypatch):
    _tsnet(monkeypatch, enabled=False)
    monkeypatch.setattr(
        service_cli, "load_registry", lambda: service_cli.ServiceRegistry((), None)
    )
    result = CliRunner().invoke(service_cli.service, ["list"])
    assert result.exit_code == 0
    assert "No local services are published." in result.output
