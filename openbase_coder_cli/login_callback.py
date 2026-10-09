"""Loopback OAuth callbacks: find the port a login URL will redirect to.

CLIs that log in through a browser (Codex, gcloud, MCP servers, Openbase's
own login) start a server on ``http://localhost:<port>/...`` and put that
address in the provider URL's ``redirect_uri``. When the browser is on the
user's phone, the redirect lands on the phone's own loopback, so Openbase
asks the phone to forward that port back to the workspace for the duration
of the login. This module is dependency-free: it runs inside the ``BROWSER``
handler and must start fast.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
REDIRECT_QUERY_KEYS = ("redirect_uri", "redirect_url")
FORWARD_MIN_PORT = 1024
FORWARD_MAX_PORT = 65535
DEFAULT_FORWARD_TTL_SECONDS = 600
FORWARD_TOKEN_BYTES = 24


def loopback_callback_port(login_url: str) -> int | None:
    """The explicit loopback port named by ``login_url``'s redirect, if any.

    Returns None when the URL has no loopback redirect, the redirect has no
    explicit port, or the port is privileged (the phone cannot bind below
    1024 and neither can the workspace listener).
    """
    try:
        query = parse_qs(urlsplit(login_url).query, keep_blank_values=False)
    except ValueError:
        return None
    for key in REDIRECT_QUERY_KEYS:
        for candidate in query.get(key, ()):
            port = _loopback_port(candidate)
            if port is not None:
                return port
    return None


def _loopback_port(redirect: str) -> int | None:
    try:
        parts = urlsplit(redirect)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    if host is None or host.lower() not in LOOPBACK_HOSTS:
        return None
    if port is None or not FORWARD_MIN_PORT <= port <= FORWARD_MAX_PORT:
        return None
    return port


@dataclass(frozen=True)
class LoopbackForward:
    """A forward the phone should run: loopback ``port`` -> ``target:port``."""

    port: int
    target: str
    ttl_seconds: int = DEFAULT_FORWARD_TTL_SECONDS
    token: str = ""

    @classmethod
    def create(
        cls, port: int, target: str, *, ttl_seconds: int = DEFAULT_FORWARD_TTL_SECONDS
    ) -> LoopbackForward:
        return cls(
            port=port,
            target=target,
            ttl_seconds=ttl_seconds,
            token=secrets.token_urlsafe(FORWARD_TOKEN_BYTES),
        )

    def as_app_control(self) -> dict[str, int | str]:
        """The ``loopback_forward`` object of an app-control ``open_url``."""
        return {
            "port": self.port,
            "target": self.target,
            "ttl_seconds": self.ttl_seconds,
            "token": self.token,
        }

    def as_push_user_info(self) -> dict[str, str]:
        """The flat string keys carried by an APNs/FCM ``open_url`` push."""
        return {
            "forward_port": str(self.port),
            "forward_target": self.target,
            "forward_ttl_seconds": str(self.ttl_seconds),
            "forward_token": self.token,
        }
