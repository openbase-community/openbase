"""`service publish` on a cloud workspace (embedded tailnet node)."""

from __future__ import annotations

import importlib
from dataclasses import replace

import pytest
from click.testing import CliRunner

from openbase_coder_cli.services import cloud_registration, tunneld
from openbase_coder_cli.services import published_service_routes as routes
from openbase_coder_cli.services import tailscale_provider as provider
from openbase_coder_cli.services.published_services import (
    HTTPS_PROXY_PORT,
    MODE_HOSTNAME,
    PublishedService,
    ServiceRegistry,
    save_registry,
)

service_cli = importlib.import_module("openbase_coder_cli.cli.service")

HOSTNAME_RULE = {
    "kind": "published-https-hostname",
    "hostname": "crm.abcd2345efgh.vpn.obs.so",
    "proxy_port": 52808,
}
CONSOLE_RULES = [{"kind": "openbase-console"}, {"kind": "openbase-livekit"}]


@pytest.fixture
def tsnet(monkeypatch):
    monkeypatch.setattr(provider, "is_netmesh_tsnet", lambda: True)
    monkeypatch.setattr(provider, "is_netmesh", lambda: True)
    monkeypatch.setattr(tunneld, "ensure_tunneld_running", lambda: None)
    monkeypatch.setattr(
        tunneld, "tunneld_health", lambda: {"reachable": True, "forwards_up": True}
    )


class FakeForwards:
    """The daemon's dynamic forward table."""

    def __init__(self, monkeypatch, live=None):
        self.live = list(live or [])
        self.added = []
        self.removed = []
        monkeypatch.setattr(tunneld, "tunneld_list_forwards", lambda: list(self.live))
        monkeypatch.setattr(tunneld, "tunneld_add_forward", self.add)
        monkeypatch.setattr(tunneld, "tunneld_remove_forward", self.remove)

    def add(self, port, **kwargs):
        self.added.append((port, kwargs))
        entry = {"port": port, **kwargs}
        self.live.append(entry)
        return entry

    def remove(self, port):
        self.removed.append(port)
        self.live = [item for item in self.live if item["port"] != port]
        return True


def test_embedded_node_advertises_hostname_routing(tsnet):
    capability = provider.serve_capability()
    assert capability["supported"] is True
    assert capability["atomic_etag"] is True
    assert capability["service_hostnames"]["https_port"] == 443


def test_embedded_node_capability_fails_closed_when_the_daemon_is_down(monkeypatch):
    monkeypatch.setattr(provider, "is_netmesh_tsnet", lambda: True)
    monkeypatch.setattr(
        tunneld, "tunneld_health", lambda: {"reachable": False, "error": "down"}
    )
    assert provider.serve_capability() == {"supported": False, "error": "down"}


def test_plan_and_snapshot_hash_the_registry_rules(tsnet, isolated_registry):
    assert provider.plan_serve(CONSOLE_RULES) == provider.plan_serve(
        CONSOLE_RULES[::-1]
    )
    assert provider.plan_serve(CONSOLE_RULES) != provider.plan_serve(
        [*CONSOLE_RULES, HOSTNAME_RULE]
    )
    snapshot = provider.serve_snapshot()
    assert snapshot["etag"] == provider.TSNET_SERVE_ETAG
    assert snapshot["hash"] == provider.plan_serve(CONSOLE_RULES)["hash"]

    save_registry(
        ServiceRegistry(
            (
                PublishedService(
                    "crm",
                    3000,
                    443,
                    52808,
                    mode=MODE_HOSTNAME,
                    hostname=HOSTNAME_RULE["hostname"],
                    node_id="7",
                ),
            ),
            None,
        )
    )
    assert (
        provider.serve_snapshot()["hash"]
        == provider.plan_serve([*CONSOLE_RULES, HOSTNAME_RULE])["hash"]
    )


def test_apply_reconciles_the_service_forwards(tsnet, monkeypatch):
    forwards = FakeForwards(monkeypatch)
    result = provider.apply_serve(
        [*CONSOLE_RULES, HOSTNAME_RULE], expected_etag="x", expected_hash="y"
    )
    assert result == provider.plan_serve([*CONSOLE_RULES, HOSTNAME_RULE])
    assert forwards.added == [
        (443, {"local_port": HTTPS_PROXY_PORT, "persistent": True}),
        (80, {"redirect_https": True, "persistent": True}),
    ]

    # Idempotent while the forwards are live; removed once no hostname is published.
    forwards.added.clear()
    provider.apply_serve([*CONSOLE_RULES, HOSTNAME_RULE])
    assert forwards.added == []
    provider.apply_serve(CONSOLE_RULES)
    assert forwards.removed == [443, 80]
    assert forwards.live == []


def test_apply_refuses_to_take_a_foreign_443_forward(tsnet, monkeypatch):
    FakeForwards(
        monkeypatch, live=[{"port": 443, "local_port": 443, "persistent": False}]
    )
    with pytest.raises(RuntimeError, match="port 443"):
        provider.apply_serve([*CONSOLE_RULES, HOSTNAME_RULE])


def test_serve_status_reports_the_service_forwards(tsnet, monkeypatch):
    monkeypatch.setattr(
        tunneld,
        "tunneld_health",
        lambda: {
            "reachable": True,
            "forwards_up": True,
            "self_dns_name": "ws.net.obs.so.",
        },
    )
    FakeForwards(
        monkeypatch,
        live=[
            {"port": 443, "local_port": HTTPS_PROXY_PORT, "persistent": True},
            {"port": 80, "redirect_https": True, "persistent": True},
            {"port": 3000, "local_port": 3000, "persistent": False},
        ],
    )
    status = provider.serve_status_json()
    assert status["TCP"]["443"] == {"TCPForward": f"127.0.0.1:{HTTPS_PROXY_PORT}"}
    assert status["TCP"]["80"] == {"HTTPS": True}
    assert "3000" not in status["TCP"]


def test_workspace_allocates_its_own_node_and_resolves_through_the_daemon(
    tsnet, monkeypatch
):
    monkeypatch.setattr(
        provider,
        "status_json",
        lambda: {
            "Self": {
                "DNSName": "devspace-abc.net.obs.so.",
                "TailscaleIPs": ["100.64.0.10"],
            }
        },
    )
    monkeypatch.setattr(
        provider,
        "hostname_serve_capability",
        lambda: {"supported": True, "https_port": 443, "https_supported": True},
    )
    posted = []

    def allocate(*, node_id, service_name):
        posted.append((node_id, service_name))
        return cloud_registration.CloudReportResult(
            ok=True,
            supported=True,
            response={
                "hostname": "crm.abcd2345efgh.vpn.obs.so",
                "node_id": "7",
                "created": True,
            },
        )

    monkeypatch.setattr(
        cloud_registration, "allocate_netmesh_service_hostname", allocate
    )
    monkeypatch.setattr(
        cloud_registration,
        "list_netmesh_devices",
        lambda: pytest.fail("the device list needs an owner login"),
    )
    resolved = []
    monkeypatch.setattr(
        tunneld,
        "tunneld_resolve",
        lambda name: resolved.append(name) or ["100.64.0.10"],
    )

    allocation = routes.allocate_private_service_hostname("crm")

    assert posted == [("self", "crm")]
    assert allocation == routes.ServiceHostnameAllocation(
        "crm.abcd2345efgh.vpn.obs.so", "7", True
    )
    assert resolved == ["crm.abcd2345efgh.vpn.obs.so"]


def test_workspace_rolls_back_when_the_daemon_cannot_resolve_the_hostname(
    tsnet, monkeypatch
):
    monkeypatch.setattr(
        provider,
        "status_json",
        lambda: {
            "Self": {
                "DNSName": "devspace-abc.net.obs.so.",
                "TailscaleIPs": ["100.64.0.10"],
            }
        },
    )
    monkeypatch.setattr(
        provider,
        "hostname_serve_capability",
        lambda: {"supported": True, "https_port": 443, "https_supported": True},
    )
    monkeypatch.setattr(
        cloud_registration,
        "allocate_netmesh_service_hostname",
        lambda **_kwargs: cloud_registration.CloudReportResult(
            ok=True,
            supported=True,
            response={
                "hostname": "crm.abcd2345efgh.vpn.obs.so",
                "node_id": "7",
                "created": True,
            },
        ),
    )
    released = []
    monkeypatch.setattr(
        cloud_registration,
        "release_netmesh_service_hostname",
        lambda **kwargs: (
            released.append(kwargs)
            or cloud_registration.CloudReportResult(ok=True, supported=True)
        ),
    )

    def failing_resolve(name):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(tunneld, "tunneld_resolve", failing_resolve)
    monkeypatch.setattr(routes, "HOSTNAME_DNS_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(routes.HostnamePublicationUnavailable, match="lookup failed"):
        routes.allocate_private_service_hostname("crm")
    assert released == [{"node_id": "7", "service_name": "crm"}]


def test_publish_on_a_workspace_persists_without_launchd(
    tsnet, monkeypatch, isolated_registry
):
    monkeypatch.setattr(
        "openbase_coder_cli.services.service_certificates.ensure_certificate",
        lambda service: None,
    )
    monkeypatch.setattr(service_cli, "local_service_available", lambda _port: True)
    monkeypatch.setattr(service_cli, "allocate_hostname_proxy", lambda: 52808)
    monkeypatch.setattr(
        service_cli,
        "allocate_private_service_hostname",
        lambda name: routes.ServiceHostnameAllocation(
            f"{name}.abcd2345efgh.vpn.obs.so", "7", False
        ),
    )
    monkeypatch.setattr(
        service_cli,
        "install_launchd_service",
        lambda _item: pytest.fail("a workspace has no launchd"),
    )
    started = []
    monkeypatch.setattr(
        service_cli,
        "start_ephemeral_gateway",
        lambda item: started.append(item) or 4242,
    )
    monkeypatch.setattr(service_cli, "gateway_healthy", lambda _item: True)
    monkeypatch.setattr(service_cli, "apply_route", lambda _item, **_kwargs: "h1")
    monkeypatch.setattr(service_cli, "service_url", lambda _item: "https://crm.x/")

    result = CliRunner().invoke(
        service_cli.service, ["publish", "crm", "3000", "--persist"]
    )

    assert result.exit_code == 0, result.output
    assert started and started[0].persistent is True
    assert "workspace start" in result.output
    stored = service_cli.load_registry().services[0]
    assert stored.pid == 4242 and stored.persistent is True


def test_restore_restarts_persistent_gateways_and_routes(
    tsnet, monkeypatch, isolated_registry
):
    crm = PublishedService(
        "crm",
        3000,
        443,
        52808,
        persistent=True,
        pid=1,
        mode=MODE_HOSTNAME,
        hostname=HOSTNAME_RULE["hostname"],
        node_id="7",
    )
    session_only = replace(crm, name="tmp", proxy_port=52809, persistent=False)
    save_registry(ServiceRegistry((crm, session_only), "h0"))
    certificates = []
    monkeypatch.setattr(
        "openbase_coder_cli.services.service_certificates.ensure_certificate",
        certificates.append,
    )
    monkeypatch.setattr(
        service_cli, "gateway_healthy", lambda item, timeout=3.0: item.pid == 77
    )
    monkeypatch.setattr(service_cli, "start_ephemeral_gateway", lambda item: 77)
    reconciled = []
    monkeypatch.setattr(
        "openbase_coder_cli.services.published_service_routes.reconcile_openbase_routes",
        lambda previous, desired, last: (
            reconciled.append((previous, desired, last)) or "h1"
        ),
    )

    result = CliRunner().invoke(service_cli.service, ["restore"])

    assert result.exit_code == 0, result.output
    assert "Restored crm." in result.output
    assert [item.name for item in certificates] == ["crm"]
    registry = service_cli.load_registry()
    assert registry.services[0].pid == 77
    assert registry.services[1].pid == 1  # session-only publications stay as they were
    assert registry.last_applied_serve_hash == "h1"
    assert reconciled[0][2] == "h0"


def test_restore_needs_the_embedded_node(monkeypatch):
    monkeypatch.setattr(provider, "is_netmesh_tsnet", lambda: False)
    result = CliRunner().invoke(service_cli.service, ["restore"])
    assert result.exit_code != 0
    assert "service publish" in result.output
