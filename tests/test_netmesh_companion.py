from __future__ import annotations

import sys
from pathlib import Path

import pytest

from openbase_coder_cli.services import netmesh_companion as nc


def test_app_candidates_prefer_shipping_then_in_repo_dev(tmp_path: Path) -> None:
    workspace = tmp_path / "openbase-coder-workspace"
    candidates = [str(c) for c in nc._companion_app_candidates(workspace)]
    # Shipping (installed desktop app) is first.
    assert candidates[0] == (
        "/Applications/Openbase.app/Contents/Resources/OpenbaseNetmeshCompanion.app"
    )
    # Dev fallbacks point at the in-repo desktop/ project (not headscale-clients).
    assert any(
        c.endswith("desktop/companion-build/OpenbaseNetmeshCompanion.app")
        for c in candidates
    )
    assert any("desktop/netmesh-macos/DerivedData" in c for c in candidates)
    assert not any("headscale-clients" in c for c in candidates)


def test_app_candidates_without_workspace_only_shipping() -> None:
    assert len(nc._companion_app_candidates(None)) == 1


def test_find_companion_app_returns_existing(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    built = workspace / "desktop" / "companion-build" / "OpenbaseNetmeshCompanion.app"
    built.mkdir(parents=True)
    assert nc._find_companion_app(workspace) == built


def test_find_companion_app_missing_returns_none(tmp_path: Path) -> None:
    assert nc._find_companion_app(tmp_path) is None


def test_companion_status_running_and_helper_flags() -> None:
    if sys.platform != "darwin":
        pytest.skip("NetmeshCompanion is macOS-only")
    companion = nc.NetmeshCompanion()
    connected = companion._parse_status(
        {
            "backendState": "Running",
            "helper": "enabled",
            "selfIP": "100.64.0.14",
            "dnsName": "gabes-macbook-pro-openbase.netmesh.openbase.cloud.",
        }
    )
    assert connected.running is True
    assert connected.helper_enabled is True
    assert connected.self_ip == "100.64.0.14"
    # Trailing dot trimmed.
    assert connected.dns_name == "gabes-macbook-pro-openbase.netmesh.openbase.cloud"

    stopped = companion._parse_status(
        {"backendState": "Stopped", "helper": "requires-approval"}
    )
    assert stopped.running is False
    assert stopped.helper_enabled is False


def test_non_darwin_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nc.sys, "platform", "linux")
    with pytest.raises(nc.NetmeshCompanionError, match="macOS-only"):
        nc.NetmeshCompanion()
