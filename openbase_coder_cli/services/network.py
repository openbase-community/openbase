"""Cross-platform Tailscale/LAN network discovery for service runners.

Replaces the previous per-OS shell parsing (``ifconfig``/``route``/``ip``/
``ipconfig``) embedded in the bash ``command_template`` bodies with a single
``psutil``/``socket``-backed implementation used on macOS, Linux, and
Windows alike.
"""

from __future__ import annotations

import ipaddress
import platform
import shutil
import socket
import subprocess

import psutil

# App Store Tailscale keeps its CLI inside the app bundle, off every PATH.
_TAILSCALE_MACOS_APP_BUNDLE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def _tailscale_binary() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    if platform.system() == "Darwin":
        from pathlib import Path

        if Path(_TAILSCALE_MACOS_APP_BUNDLE).is_file():
            return _TAILSCALE_MACOS_APP_BUNDLE
    return None


def tailscale_ip(family: str = "4") -> str | None:
    """The node's Tailscale IPv4 (``family="4"``) or IPv6 address."""
    binary = _tailscale_binary()
    if not binary:
        return None
    result = subprocess.run(
        [binary, "ip", f"-{family}"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate:
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
