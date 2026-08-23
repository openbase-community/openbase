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

# Where the netmesh client bundles the shim (overridable for dev installs).
NETMESH_CTL_DEFAULT = "/Applications/OpenbaseNetmesh.app/Contents/MacOS/netmesh-ctl"

_TAILSCALE_FALLBACK_PATHS = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


def provider() -> str:
    """The active provider name (defaults to ``tailscale``)."""
    value = (
        (os.environ.get("OPENBASE_CODER_CLI_TAILSCALE_PROVIDER") or "").strip().lower()
    )
    return value if value in PROVIDER_VALUES else PROVIDER_TAILSCALE


def is_netmesh() -> bool:
    """True for either netmesh transport (VPN or embedded tsnet) — both ride the
    self-hosted headscale control plane and share netmesh labelling/hostnames."""
    return provider() in (PROVIDER_NETMESH, PROVIDER_NETMESH_TSNET)


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
    if os.access(NETMESH_CTL_DEFAULT, os.X_OK):
        return NETMESH_CTL_DEFAULT
    return shutil.which("netmesh-ctl")


# The embedded no-VPN transport is reached via openbase-tunneld's loopback
# control API, not the netmesh-ctl shim. That client is staged separately
# (option 3 / openbase#10), so until it lands the tsnet provider records the
# selection but its runtime ops report this instead of using the VPN shim.
TSNET_PENDING_ERROR = (
    "Embedded netmesh (tsnet, no-VPN) transport is selected but its control "
    "daemon (openbase-tunneld) is not wired up in this build yet."
)


def tool_path() -> str | None:
    """Path to the active provider's control tool, or None if not installed."""
    if is_netmesh_tsnet():
        return None  # openbase-tunneld control client is staged (see above)
    return netmesh_ctl_bin() if is_netmesh() else tailscale_bin()


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
        return {"error": TSNET_PENDING_ERROR}
    if is_netmesh():
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
        return {"error": TSNET_PENDING_ERROR}
    if is_netmesh():
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
        raise RuntimeError(TSNET_PENDING_ERROR)
    if is_netmesh():
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
