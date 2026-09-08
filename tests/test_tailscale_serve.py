from __future__ import annotations

import subprocess

import pytest

from openbase_coder_cli.services import tailscale_provider as tp
from openbase_coder_cli.services import tailscale_serve


def test_configure_tailscale_serve_installs_openbase_and_livekit_routes(monkeypatch):
    # Default (official tailscale) provider: serve is applied through the
    # provider abstraction, which shells out to the tailscale CLI.
    monkeypatch.delenv("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER", raising=False)
    commands = []

    monkeypatch.setattr(tp, "tailscale_bin", lambda: "/usr/bin/tailscale")

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(tp.subprocess, "run", fake_run)

    tailscale_serve.configure_tailscale_serve()

    assert [command for command, _kwargs in commands] == [
        [
            "/usr/bin/tailscale",
            "serve",
            "--bg",
            "--http=18080",
            "http://127.0.0.1:7999",
        ],
        [
            "/usr/bin/tailscale",
            "serve",
            "--bg",
            "--tcp=7880",
            "tcp://127.0.0.1:7880",
        ],
    ]


def test_configure_tailscale_serve_bootstraps_fresh_netmesh_helper(
    monkeypatch, tmp_path
):
    # A brand-new hardened helper reports its empty config and no last-applied
    # hash is recorded yet, so the first apply must go through instead of
    # tripping the CAS drift guard.
    from openbase_coder_cli.services import published_services as published

    monkeypatch.setattr(
        published, "PUBLISHED_SERVICES_PATH", tmp_path / "published-services.json"
    )
    monkeypatch.setattr(tp, "is_netmesh", lambda: True)
    monkeypatch.setattr(tp, "is_netmesh_tsnet", lambda: False)
    monkeypatch.setattr(tp, "netmesh_uses_stock_tailscale", lambda: False)
    monkeypatch.setattr(tp, "serve_capability", lambda: {"supported": True})
    monkeypatch.setattr(tp, "serve_snapshot", lambda: {"etag": "v1", "hash": "empty"})
    monkeypatch.setattr(
        tp,
        "plan_serve",
        lambda rules: {"hash": "empty" if rules == [] else "baseline"},
    )
    applied = []
    monkeypatch.setattr(
        tp,
        "apply_serve",
        lambda rules, **kwargs: applied.append((rules, kwargs)) or {"hash": "next"},
    )

    tailscale_serve.configure_tailscale_serve()

    assert applied[0][1] == {"expected_etag": "v1", "expected_hash": "empty"}
    assert published.load_registry().last_applied_serve_hash == "next"


def test_reset_tailscale_serve_recovers_from_drift_dead_end(monkeypatch, tmp_path):
    """configure refuses when the live config drifted from the recorded hash;
    reset must recover by re-applying over the LIVE snapshot as the CAS base."""
    from openbase_coder_cli.services import published_services as published

    monkeypatch.setattr(
        published, "PUBLISHED_SERVICES_PATH", tmp_path / "published-services.json"
    )
    # A recorded last-applied hash the live config no longer matches -> drift.
    published.save_registry(published.ServiceRegistry({}, "stale-recorded-hash"))

    monkeypatch.setattr(tp, "is_netmesh", lambda: True)
    monkeypatch.setattr(tp, "is_netmesh_tsnet", lambda: False)
    monkeypatch.setattr(tp, "netmesh_uses_stock_tailscale", lambda: False)
    monkeypatch.setattr(tp, "serve_capability", lambda: {"supported": True})
    monkeypatch.setattr(
        tp, "serve_snapshot", lambda: {"etag": "live-etag", "hash": "live-drifted"}
    )
    applied: list = []
    monkeypatch.setattr(
        tp,
        "apply_serve",
        lambda rules, **kwargs: applied.append((rules, kwargs))
        or {"hash": "reset-hash"},
    )

    # The default path dead-ends on this drift...
    with pytest.raises(RuntimeError, match="drifted"):
        tailscale_serve.configure_tailscale_serve()

    # ...but reset recovers, using the live snapshot (not the stale hash) as base.
    tailscale_serve.reset_tailscale_serve()

    assert applied[-1][1] == {
        "expected_etag": "live-etag",
        "expected_hash": "live-drifted",
    }
    assert published.load_registry().last_applied_serve_hash == "reset-hash"


def test_tailscale_serve_health_requires_routes_and_external_health(monkeypatch):
    monkeypatch.delenv("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER", raising=False)
    monkeypatch.setattr(tp, "tool_path", lambda: "/usr/bin/tailscale")
    monkeypatch.setattr(
        tp,
        "status_json",
        lambda: {
            "Self": {
                "DNSName": "mac.tailnet.ts.net.",
                "TailscaleIPs": ["100.64.0.9", "fd7a:115c:a1e0::9"],
            }
        },
    )
    monkeypatch.setattr(
        tp,
        "serve_status_json",
        lambda: {
            "TCP": {
                "18080": {"HTTP": True},
                "7880": {"TCPForward": "127.0.0.1:7880"},
            },
            "Web": {
                "mac.tailnet.ts.net:18080": {
                    "Handlers": {
                        "/": {"Proxy": "http://127.0.0.1:7999"},
                    }
                }
            },
        },
    )
    # Reachability is probed via the tailnet IP with the MagicDNS name in the
    # Host header (DNS-independent, but still matches the name-based serve mount).
    monkeypatch.setattr(
        tailscale_serve,
        "_openbase_reachable",
        lambda url, host_header=None: (
            url == "http://100.64.0.9:18080"
            and host_header == "mac.tailnet.ts.net:18080",
            None,
        ),
    )

    health = tailscale_serve.tailscale_serve_health()

    assert health.healthy is True
    assert health.host == "mac.tailnet.ts.net"
    assert health.openbase_url == "http://mac.tailnet.ts.net:18080"
