"""Cross-platform Tailscale/LAN network discovery for service runners.

Replaces the previous per-OS shell parsing (``ifconfig``/``route``/``ip``/
``ipconfig``) embedded in the bash ``command_template`` bodies with a single
``psutil``/``socket``-backed implementation used on macOS, Linux, and
Windows alike.
"""

from __future__ import annotations

import ipaddress
import socket

import psutil


# App Store Tailscale keeps its CLI inside the app bundle, off every PATH.
def tailscale_ip(family: str = "4") -> str | None:
    """The node's tailnet IPv4 (``family="4"``) or IPv6 address.

    Routed through the provider abstraction so it answers for whichever
    transport is active (Tailscale app, netmesh VPN, or the embedded node)
    rather than assuming the official daemon.
    """
    from openbase_coder_cli.services import tailscale_provider as tp

    payload = tp.status_json()
    for ip in (payload.get("Self") or {}).get("TailscaleIPs") or []:
        candidate = str(ip).strip()
        if not candidate:
            continue
        if (family == "4" and "." in candidate) or (family == "6" and ":" in candidate):
            return candidate
    return None


def resolve_interface(ip: str) -> str | None:
    """Name of the local interface holding address ``ip``, if any."""
    try:
        target = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            # Strip an IPv6 zone id (e.g. "fe80::1%en0") before comparing.
            if addr.address.split("%")[0] == str(target):
                return iface
    return None


def default_lan_ip() -> str | None:
    """Best-effort primary outbound LAN IPv4 address."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
