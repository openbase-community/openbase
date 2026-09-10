from __future__ import annotations

import json

import pytest

from openbase_coder_cli.services import tailscale_provider as provider


def test_rule_validation_rejects_arbitrary_targets_and_paths():
    with pytest.raises(ValueError, match="only a validated hostname"):
        provider._validated_rule(
            {
                "kind": "published-hostname",
                "hostname": "crm.mac.net.obs.so",
                "proxy_port": 52808,
                "target": "http://attacker.example",
            }
        )
    with pytest.raises(ValueError, match="Unsupported"):
        provider._validated_rule(
            {"kind": "raw", "path": "/", "target": "http://127.0.0.1:22"}
        )


def test_atomic_apply_passes_validated_rules_etag_and_hash(monkeypatch):
    commands = []
    monkeypatch.setattr(provider, "is_netmesh_tsnet", lambda: False)
    monkeypatch.setattr(provider, "is_netmesh", lambda: True)
    monkeypatch.setattr(provider, "netmesh_uses_stock_tailscale", lambda: False)
    monkeypatch.setattr(provider, "netmesh_ctl_bin", lambda: "/signed/netmesh-ctl")
    monkeypatch.setattr(
        provider,
        "_parsed",
        lambda command: commands.append(command) or {"hash": "after", "etag": "v2"},
    )

    result = provider.apply_serve(
        [
            {"kind": "openbase-console"},
            {"kind": "openbase-livekit"},
            {
                "kind": "published-hostname",
                "hostname": "crm.mac.net.obs.so",
                "proxy_port": 52808,
            },
        ],
        expected_etag="v1",
        expected_hash="before",
    )

    assert result["hash"] == "after"
    assert commands[0][0:2] == ["/signed/netmesh-ctl", "serve-apply"]
    assert json.loads(commands[0][2])[-1] == {
        "kind": "published-hostname",
        "hostname": "crm.mac.net.obs.so",
        "proxy_port": 52808,
    }
    assert commands[0][3:] == ["v1", "before"]


def test_atomic_apply_requires_compare_and_swap_values(monkeypatch):
    monkeypatch.setattr(provider, "is_netmesh_tsnet", lambda: False)
    monkeypatch.setattr(provider, "is_netmesh", lambda: True)
    monkeypatch.setattr(provider, "netmesh_uses_stock_tailscale", lambda: False)

    with pytest.raises(RuntimeError, match="ETag"):
        provider.apply_serve([{"kind": "openbase-console"}])


def test_existing_helper_capability_does_not_imply_hostname_dns(monkeypatch):
    monkeypatch.setattr(
        provider,
        "serve_capability",
        lambda: {
            "supported": True,
            "atomic_etag": True,
            "http_port": 80,
            "cert_domains": None,
        },
    )

    capability = provider.hostname_serve_capability()

    assert capability["supported"] is False
    assert "does not advertise" in capability["error"]


def test_hostname_capability_honors_helper_kill_switch(monkeypatch):
    from openbase_coder_cli.services import cloud_registration

    declared = {
        "supported": False,
        "dns_allocation": False,
        "serve_routing": True,
        "pattern": "{service}.{account_namespace}.{service_domain}",
        "http_port": 80,
        "https_port": 443,
        "https_supported": True,
    }
    monkeypatch.setattr(
        provider,
        "serve_capability",
        lambda: {
            "supported": True,
            "atomic_etag": True,
            "service_hostnames": declared,
        },
    )
    monkeypatch.setattr(
        cloud_registration,
        "netmesh_service_hostname_capabilities",
        lambda: cloud_registration.CloudReportResult(
            ok=True,
            supported=True,
            response={
                "supported": True,
                "dns_allocation": True,
                "account_private_dns": True,
                "serve_routing": False,
                "pattern": "{service}.{account_namespace}.{service_domain}",
                "http_port": 80,
                "https_port": 443,
                "https_supported": True,
            },
        ),
    )

    capability = provider.hostname_serve_capability()
    assert capability["supported"] is False
    assert "explicitly disabled" in capability["error"]

    declared["supported"] = True
    assert provider.hostname_serve_capability() == {
        "supported": True,
        "dns_allocation": True,
        "serve_routing": True,
        "pattern": "{service}.{account_namespace}.{service_domain}",
        "http_port": 80,
        "https_port": 443,
        "https_supported": True,
    }


class _FakeStatusResult:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout


def _stock_status(monkeypatch, backend_state: str | None, *, returncode: int = 0):
    monkeypatch.setattr(provider, "tailscale_bin", lambda: "/stock/tailscale")
    payload = "" if backend_state is None else json.dumps({"BackendState": backend_state})
    monkeypatch.setattr(
        provider.subprocess,
        "run",
        lambda *a, **k: _FakeStatusResult(returncode, payload),
    )


def test_stock_tailscale_conflict_flags_active_vpn_on_netmesh(monkeypatch):
    monkeypatch.setattr(provider, "provider", lambda: provider.PROVIDER_NETMESH)
    monkeypatch.setattr(provider, "netmesh_uses_stock_tailscale", lambda: False)
    _stock_status(monkeypatch, "Running")
    assert "official Tailscale VPN" in (provider.stock_tailscale_conflict() or "")


def test_stock_tailscale_installed_but_stopped_is_not_a_conflict(monkeypatch):
    monkeypatch.setattr(provider, "provider", lambda: provider.PROVIDER_NETMESH)
    monkeypatch.setattr(provider, "netmesh_uses_stock_tailscale", lambda: False)
    _stock_status(monkeypatch, "Stopped")
    assert provider.stock_tailscale_conflict() is None


def test_stock_tailscale_absent_is_not_a_conflict(monkeypatch):
    monkeypatch.setattr(provider, "provider", lambda: provider.PROVIDER_NETMESH)
    monkeypatch.setattr(provider, "netmesh_uses_stock_tailscale", lambda: False)
    monkeypatch.setattr(provider, "tailscale_bin", lambda: None)
    assert provider.stock_tailscale_conflict() is None


def test_stock_tailscale_conflict_ignored_on_stock_transport(monkeypatch):
    monkeypatch.setattr(provider, "provider", lambda: provider.PROVIDER_TAILSCALE)
    _stock_status(monkeypatch, "Running")
    assert provider.stock_tailscale_conflict() is None


def test_stock_tailscale_conflict_ignored_where_netmesh_rides_stock(monkeypatch):
    monkeypatch.setattr(provider, "provider", lambda: provider.PROVIDER_NETMESH)
    monkeypatch.setattr(provider, "netmesh_uses_stock_tailscale", lambda: True)
    _stock_status(monkeypatch, "Running")
    assert provider.stock_tailscale_conflict() is None


def test_stock_tailscale_probe_failure_is_not_a_conflict(monkeypatch):
    monkeypatch.setattr(provider, "provider", lambda: provider.PROVIDER_NETMESH)
    monkeypatch.setattr(provider, "netmesh_uses_stock_tailscale", lambda: False)
    _stock_status(monkeypatch, None, returncode=1)
    assert provider.stock_tailscale_conflict() is None
