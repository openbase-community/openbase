from __future__ import annotations

import sys
from pathlib import Path

import pytest

from openbase_coder_cli.services import netmesh_companion as nc


def test_app_candidates_prefer_in_repo_dev_then_shipping(tmp_path: Path) -> None:
    workspace = tmp_path / "openbase-coder-workspace"
    candidates = [str(c) for c in nc._companion_app_candidates(workspace)]
    # Developer installs use the recorded workspace's matching control shim.
    assert candidates[0].endswith(
        "desktop/companion-build/OpenbaseNetmeshCompanion.app"
    )
    # The installed desktop app remains the final fallback.
    assert candidates[-1] == (
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


def test_missing_build_tools_lists_absent_and_skips_go_when_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    workspace = tmp_path / "ws"
    # Only xcodegen/xcodebuild present; node + go absent.
    present = {"xcodegen", "xcodebuild"}
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/usr/bin/{name}" if name in present else None
    )
    missing = nc._missing_build_tools(workspace)
    assert any(m.startswith("node") for m in missing)
    assert any(m.startswith("go") for m in missing)  # engine not staged -> go needed

    # Stage the tailscale engine -> go no longer required.
    vendor = workspace / "desktop" / "netmesh-macos" / "vendor" / "tailscale-bin"
    vendor.mkdir(parents=True)
    (vendor / "tailscaled").write_text("")
    (vendor / "tailscale").write_text("")
    missing_staged = nc._missing_build_tools(workspace)
    assert not any(m.startswith("go") for m in missing_staged)


def test_build_companion_fails_fast_with_prereq_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    workspace = tmp_path / "ws"
    stage = workspace / "desktop" / "scripts" / "stage-netmesh-companion.mjs"
    stage.parent.mkdir(parents=True)
    stage.write_text("// stage script")
    monkeypatch.setattr(shutil, "which", lambda name: None)  # nothing installed
    with pytest.raises(nc.NetmeshCompanionError, match="needs these tools first"):
        nc._build_companion(workspace)


def test_netmesh_ctl_path_finds_dev_companion_shim(tmp_path: Path) -> None:
    """Regression: on a dev install the shim lives in desktop/companion-build,
    which the serve-rules step must find (it previously only looked in
    /Applications and reported 'netmesh-ctl was not found')."""
    workspace = tmp_path / "ws"
    macos = (
        workspace
        / "desktop"
        / "companion-build"
        / "OpenbaseNetmeshCompanion.app"
        / "Contents"
        / "MacOS"
    )
    macos.mkdir(parents=True)
    shim = macos / "netmesh-ctl"
    shim.write_text("#!/bin/sh\n")
    assert nc.netmesh_ctl_path(workspace) == str(shim)


def test_netmesh_ctl_path_none_when_absent(tmp_path: Path) -> None:
    assert nc.netmesh_ctl_path(tmp_path / "empty-ws") is None


def test_capability_error_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib

    t = importlib.import_module("openbase_coder_cli.cli.tailnet")
    tp = importlib.import_module("openbase_coder_cli.services.tailscale_provider")

    # tailscale: blocked without a client binary.
    monkeypatch.setattr(tp, "tailscale_bin", lambda: None)
    assert "Tailscale client is not installed" in (
        t._capability_error("tailscale") or ""
    )
    monkeypatch.setattr(tp, "tailscale_bin", lambda: "/usr/bin/tailscale")
    assert t._capability_error("tailscale") is None

    # netmesh on non-mac (stock-tailscale path): no companion requirement.
    monkeypatch.setattr(tp, "netmesh_uses_stock_tailscale", lambda: True)
    assert t._capability_error("netmesh") is None

    # netmesh on macOS without companion or workspace: blocked with guidance.
    monkeypatch.setattr(tp, "netmesh_uses_stock_tailscale", lambda: False)
    monkeypatch.setattr(t, "_dev_workspace_dir_or_none", lambda: None)
    monkeypatch.setattr(nc, "_find_companion_app", lambda ws: None)
    blocked = t._capability_error("netmesh")
    assert blocked is not None and "desktop app" in blocked

    # tsnet: never blocked.
    assert t._capability_error("netmesh-tsnet") is None


def test_revoke_old_node_matches_offline_by_captured_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    t = importlib.import_module("openbase_coder_cli.cli.tailnet")
    cr = importlib.import_module("openbase_coder_cli.services.cloud_registration")

    revoked: list[str] = []
    devices = [
        {"id": "7", "name": "gabes-mac-mini-openbase", "online": True},
        {"id": "9", "name": "gabes-mac-mini-openbase", "online": False},
    ]
    monkeypatch.setattr(cr, "list_netmesh_devices", lambda: devices)
    monkeypatch.setattr(
        cr, "revoke_netmesh_device", lambda node_id: revoked.append(node_id) or True
    )

    t._revoke_old_node(
        "netmesh-tsnet",
        {"HostName": "gabes-mac-mini-openbase", "DNSName": ""},
    )
    # Prefers the OFFLINE match, never the live node.
    assert revoked == ["9"]


def test_revoke_old_node_skips_official_tailscale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    t = importlib.import_module("openbase_coder_cli.cli.tailnet")
    cr = importlib.import_module("openbase_coder_cli.services.cloud_registration")
    monkeypatch.setattr(
        cr, "list_netmesh_devices", lambda: (_ for _ in ()).throw(AssertionError)
    )
    # Leaving the official Tailscale network revokes nothing (no API call).
    t._revoke_old_node("tailscale", {"HostName": "x"})
