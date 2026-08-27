"""Tailnet provider abstraction.

openbase-coder can ride on either the official **Tailscale** client or Openbase's
own **netmesh** (self-hosted headscale + a hardened macOS client). They are not
usable at the same time; the active one is selected by the
``OPENBASE_CODER_CLI_TAILSCALE_PROVIDER`` env var (``tailscale`` by default, or
``netmesh``). Set it at setup and switch it later via the CLI.

The two providers are reached differently, so this module abstracts the three
operations coder needs (status/serve-status/apply-serve) rather than swapping a
binary:

* ``tailscale``  → the ``tailscale`` CLI on the default socket (unchanged
  behaviour; fully backwards compatible).
* ``netmesh``    → the signed ``netmesh-ctl`` shim, which reaches the hardened
  root daemon over code-signature-verified XPC. coder never touches netmesh's
  root-only tailscaled socket.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

TIMEOUT_SECONDS = 5

PROVIDER_TAILSCALE = "tailscale"
PROVIDER_NETMESH = "netmesh"
# Same self-hosted headscale as ``netmesh``, but joined by an in-process embedded
# node (tsnet on the Mac via openbase-tunneld; TailscaleKit on iOS) instead of a
# device-wide VPN — so NO system extension / VPN profile is needed on either end
# and only the Openbase apps' own traffic rides the tailnet. The embedded
# transport (tunneld) is staged separately; selecting this records the choice.
PROVIDER_NETMESH_TSNET = "netmesh-tsnet"

PROVIDER_VALUES = (PROVIDER_TAILSCALE, PROVIDER_NETMESH, PROVIDER_NETMESH_TSNET)

# Where the netmesh VPN machinery bundles the shim, most-preferred first:
# the companion nested in the Openbase desktop app (the shipping layout — the
# standalone Openbase Netmesh app is retired), then the legacy standalone app.
NETMESH_CTL_CANDIDATES = (
    "/Applications/Openbase.app/Contents/Resources/OpenbaseNetmeshCompanion.app"
    "/Contents/MacOS/netmesh-ctl",
    "/Applications/OpenbaseNetmesh.app/Contents/MacOS/netmesh-ctl",
)

_TAILSCALE_FALLBACK_PATHS = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


def provider() -> str:
    """The active provider name (defaults to ``tailscale``).

    The process env wins (launchd service wrappers source the installed env
    file before exec'ing), but interactive CLI invocations never source it —
    so fall back to reading ``~/.openbase/.env`` directly. Without this,
    ``tailnet status`` and every provider-routed operation silently ran in
    tailscale mode from a bare shell regardless of the configured transport.
    """
    value = (
        (os.environ.get("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER") or "").strip().lower()
    )
    if value in PROVIDER_VALUES:
        return value
    try:
        from openbase_coder_cli.env_file import env_file_values
        from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH

        file_value = (
            env_file_values(DEFAULT_ENV_FILE_PATH)
            .get("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER", "")
            .strip()
            .lower()
        )
    except Exception:  # noqa: BLE001 - unreadable env file means default
        return PROVIDER_TAILSCALE
    return file_value if file_value in PROVIDER_VALUES else PROVIDER_TAILSCALE


def is_netmesh() -> bool:
    """True for either netmesh transport (VPN or embedded tsnet) — both ride the
    self-hosted headscale control plane and share netmesh labelling/hostnames."""
    return provider() in (PROVIDER_NETMESH, PROVIDER_NETMESH_TSNET)


def netmesh_uses_stock_tailscale() -> bool:
    """True when the netmesh VPN rides the stock tailscale client.

    The hardened netmesh client (netmesh-ctl + root daemon) is macOS-only.
    Windows and Linux join the same self-hosted headscale with the official
    tailscale client (``tailscale login --login-server``), so every netmesh
    control operation on those hosts is a plain tailscale operation.
    """
    import platform as _platform

    return _platform.system() != "Darwin"


def is_netmesh_tsnet() -> bool:
    """True only for the embedded, no-VPN netmesh transport (openbase-tunneld)."""
    return provider() == PROVIDER_NETMESH_TSNET


def tailscale_bin() -> str | None:
    """Locate the official ``tailscale`` CLI (PATH, then known install locations)."""
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in _TAILSCALE_FALLBACK_PATHS:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def netmesh_ctl_bin() -> str | None:
    override = os.environ.get("OPENBASE_CODER_CLI_NETMESH_CTL")
    if override and os.access(override, os.X_OK):
        return override
    for candidate in NETMESH_CTL_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("netmesh-ctl")


def tool_path() -> str | None:
    """Path to the active provider's control tool, or None if not installed."""
    if is_netmesh_tsnet():
        # The embedded no-VPN transport is controlled via openbase-tunneld's
        # loopback API rather than a CLI binary; "installed" means the daemon
        # binary is locatable or a daemon has already run here.
        from openbase_coder_cli.services.tunneld import tunneld_tool_path

        return tunneld_tool_path()
    if is_netmesh() and not netmesh_uses_stock_tailscale():
        return netmesh_ctl_bin()
    return tailscale_bin()


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)


def _parsed(argv: list[str]) -> dict[str, Any]:
    try:
        result = _run(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"Unable to run {argv[0]}: {exc}"}
    if result.returncode != 0:
        return {
            "error": result.stderr.strip() or result.stdout.strip() or "command failed"
        }
    out = result.stdout.strip()
    if not out:
        return {}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"error": f"Unable to parse JSON: {exc}"}
    return payload if isinstance(payload, dict) else {}


def status_json() -> dict[str, Any]:
    """Parsed node status (shape matches ``tailscale status --json``: Self, Peer…).

    Returns a dict with an ``error`` key on failure.
    """
    if is_netmesh_tsnet():
        from openbase_coder_cli.services.tunneld import tunneld_status

        _available, payload, error = tunneld_status()
        if payload is None:
            return {"error": error or "openbase-tunneld status unavailable"}
        return payload
    if is_netmesh() and not netmesh_uses_stock_tailscale():
        ctl = netmesh_ctl_bin()
        if not ctl:
            return {"error": "netmesh-ctl not found"}
        return _parsed([ctl, "status"])
    tsc = tailscale_bin()
    if not tsc:
        return {"error": "tailscale was not found on PATH."}
    return _parsed([tsc, "status", "--json"])


def serve_status_json() -> dict[str, Any]:
    """Parsed serve status (shape matches ``tailscale serve status --json``)."""
    if is_netmesh_tsnet():
        # tunneld forwards the serve routes natively; synthesize the shape the
        # tailscale-serve parsers expect from its health payload. (Serve health
        # itself takes a dedicated tunneld path in tailscale_serve.)
        from openbase_coder_cli.services.tunneld import tunneld_health

        health = tunneld_health()
        if not health.get("reachable"):
            return {
                "error": str(health.get("error") or "openbase-tunneld is not reachable")
            }
        if not health.get("forwards_up"):
            return {"error": "openbase-tunneld forwards are not up yet"}
        host = str(health.get("self_dns_name") or "").rstrip(".")
        payload: dict[str, Any] = {
            "TCP": {
                "18080": {"HTTP": True},
                "7880": {"TCPForward": "127.0.0.1:7880"},
            }
        }
        if host:
            payload["Web"] = {
                f"{host}:18080": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:7999"}}}
            }
        return payload
    if is_netmesh() and not netmesh_uses_stock_tailscale():
        ctl = netmesh_ctl_bin()
        if not ctl:
            return {"error": "netmesh-ctl not found"}
        return _parsed([ctl, "serve-status"])
    tsc = tailscale_bin()
    if not tsc:
        return {"error": "tailscale was not found on PATH."}
    return _parsed([tsc, "serve", "status", "--json"])


def apply_serve(rules: list[dict[str, Any]]) -> None:
    """Apply serve rules: ``[{"proto":"http"|"tcp","port":int,"target":str}]``.

    Raises ``RuntimeError`` on failure.
    """
    if is_netmesh_tsnet():
        # The daemon forwards 18080/7880/7881 itself; "applying serve" means
        # making sure it is running and logged in.
        from openbase_coder_cli.services.tunneld import ensure_tunneld_running

        ensure_tunneld_running()
        return
    if is_netmesh() and not netmesh_uses_stock_tailscale():
        ctl = netmesh_ctl_bin()
        if not ctl:
            raise RuntimeError("netmesh-ctl was not found.")
        result = _run([ctl, "serve-set", json.dumps(rules)])
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or result.stdout.strip() or "serve-set failed."
            )
        return
    tsc = tailscale_bin()
    if not tsc:
        raise RuntimeError("tailscale was not found.")
    for rule in rules:
        flag = f"--{rule['proto']}={rule['port']}"
        result = _run([tsc, "serve", "--bg", flag, rule["target"]])
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip()
                or result.stdout.strip()
                or "tailscale serve failed."
            )
