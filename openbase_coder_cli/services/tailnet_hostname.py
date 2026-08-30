"""This machine's netmesh tailnet hostname — the single source of truth.

Every netmesh transport (VPN companion tailscaled, embedded tunneld) MUST
enroll under the same node name: peers store this machine's MagicDNS name
(phone backend host, syncthing peer addresses, git-pointer fetch URLs), so a
transport switch that changed the name would strand every peer on a dead
address. Anything that supplies a hostname to a netmesh node goes through
here.
"""

from __future__ import annotations

import socket

TSNET_HOSTNAME_ENV_KEY = "OPENBASE_TSNET_HOSTNAME"


def netmesh_hostname() -> str:
    """Sanitized ``<host>-openbase`` tailnet name for this machine.

    Strips the DNS suffix (``Gabes-MacBook-Pro.local`` → ``gabes-macbook-pro``)
    and reduces to lowercase alphanumerics and dashes, matching what headscale
    would make of the name anyway — so the name we compute is the name peers
    actually see.
    """
    raw = socket.gethostname().split(".")[0]
    sanitized = "".join(c if c.isalnum() else "-" for c in raw.lower())
    collapsed = "-".join(part for part in sanitized.split("-") if part)
    return f"{collapsed}-openbase" if collapsed else "openbase-mac"
