from __future__ import annotations

import pytest

from openbase_coder_cli.services.tailnet_devices import (
    TailnetDevice,
    _devices_from_tailscale_status,
    _url_host_literal,
)


def test_devices_from_tailscale_status_prefers_dns_name() -> None:
    payload = {
        "Self": {
            "HostName": "macbook",
            "DNSName": "macbook.tailnet.ts.net.",
            "TailscaleIPs": ["100.1.1.1"],
            "OS": "macOS",
        },
        "Peer": {
            "node-id": {
                "HostName": "mac-mini",
                "DNSName": "mac-mini.tailnet.ts.net.",
                "TailscaleIPs": ["100.2.2.2"],
                "Online": True,
                "OS": "macOS",
            }
        },
    }

    devices = _devices_from_tailscale_status(payload)

    assert devices == [
        TailnetDevice(
            name="mac-mini",
            host="mac-mini.tailnet.ts.net",
            dns_name="mac-mini.tailnet.ts.net",
            ip="100.2.2.2",
            online=True,
            os="macOS",
        ),
        TailnetDevice(
            name="macbook",
            host="macbook.tailnet.ts.net",
            dns_name="macbook.tailnet.ts.net",
            ip="100.1.1.1",
            online=True,
            os="macOS",
            is_self=True,
        ),
    ]


def test_url_host_literal_wraps_ipv6() -> None:
    assert _url_host_literal("fd7a:115c:a1e0::1") == "[fd7a:115c:a1e0::1]"
    assert _url_host_literal("device.tailnet.ts.net") == "device.tailnet.ts.net"


def test_self_discovery_uses_local_readiness_but_advertises_vpn_url(monkeypatch):
    from openbase_coder_cli.services import tailnet_devices, tailscale_serve

    monkeypatch.setattr(
        tailscale_serve,
        "local_openbase_reachable",
        lambda host: (host == "mac.net.example.test", None),
    )
    monkeypatch.setattr(
        tailnet_devices.httpx,
        "get",
        lambda *a, **k: pytest.fail("Self must not hairpin through VPN"),
    )
    device = TailnetDevice(
        "mac",
        "mac.net.example.test",
        "mac.net.example.test",
        "100.64.0.1",
        True,
        "macOS",
        is_self=True,
    )
    tailnet_devices._probe_openbase_device(device)
    assert device.openbase_available
    assert device.openbase_url == "http://mac.net.example.test:18080"
    assert "is_self" not in device.to_dict()


def test_peer_discovery_still_probes_peer_vpn_endpoint(monkeypatch):
    import httpx

    from openbase_coder_cli.services import tailnet_devices

    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(tailnet_devices.httpx, "get", get)
    device = TailnetDevice(
        "peer",
        "peer.net.example.test",
        "peer.net.example.test",
        "100.64.0.2",
        True,
        "macOS",
    )
    tailnet_devices._probe_openbase_device(device)
    assert device.openbase_available
    assert calls == ["http://peer.net.example.test:18080/api/health/"]
