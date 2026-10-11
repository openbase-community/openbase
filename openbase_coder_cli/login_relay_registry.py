"""Live login callback relays, so the API's WebSocket bridge can find them.

A phone that cannot open a TCP connection to this host's VPN address (a host
firewall drops it) reaches the relay through the local API's VPN serve route
instead, as a WebSocket (``ws/login-relay/``). That bridge must dial only a
relay that is live right now, never an arbitrary loopback port, so each relay
records its port with a hash of the capability the phone presents and its
expiry. The capability itself is never written to disk.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from openbase_coder_cli.paths import OPENBASE_BASE_DIR

REGISTRY_DIR = OPENBASE_BASE_DIR / "login-relays"


def _digest(capability: str) -> str:
    return hashlib.sha256(capability.encode()).hexdigest()


def _entry(port: int) -> Path:
    return REGISTRY_DIR / f"{int(port)}.json"


def register(port: int, capability: str, expires_at: int) -> None:
    REGISTRY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _entry(port)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(
            {"sha256": _digest(capability), "expires_at": int(expires_at)}, handle
        )
    os.replace(temporary, path)


def unregister(port: int) -> None:
    with contextlib.suppress(FileNotFoundError):
        _entry(port).unlink()


def is_live(port: int, capability: str) -> bool:
    """True when a relay registered ``capability`` on ``port`` and it has not expired."""
    try:
        record = json.loads(_entry(port).read_text())
    except (OSError, ValueError):
        return False
    if (
        not isinstance(record, dict)
        or int(record.get("expires_at") or 0) <= time.time()
    ):
        return False
    return hmac.compare_digest(str(record.get("sha256") or ""), _digest(capability))
