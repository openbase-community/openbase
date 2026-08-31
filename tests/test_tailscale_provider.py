from __future__ import annotations

import json

import pytest

from openbase_coder_cli.services import tailscale_provider as provider


def test_rule_validation_rejects_arbitrary_targets_and_paths():
    with pytest.raises(ValueError, match="only a dispatcher port"):
        provider._validated_rule(
            {
                "kind": "portless-dispatcher",
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
            {"kind": "portless-dispatcher", "proxy_port": 52808},
        ],
        expected_etag="v1",
        expected_hash="before",
    )

    assert result["hash"] == "after"
    assert commands[0][0:2] == ["/signed/netmesh-ctl", "serve-apply"]
    assert json.loads(commands[0][2])[-1] == {
        "kind": "portless-dispatcher",
        "proxy_port": 52808,
    }
    assert commands[0][3:] == ["v1", "before"]


def test_atomic_apply_requires_compare_and_swap_values(monkeypatch):
    monkeypatch.setattr(provider, "is_netmesh_tsnet", lambda: False)
    monkeypatch.setattr(provider, "is_netmesh", lambda: True)
    monkeypatch.setattr(provider, "netmesh_uses_stock_tailscale", lambda: False)

    with pytest.raises(RuntimeError, match="ETag"):
        provider.apply_serve([{"kind": "openbase-console"}])
