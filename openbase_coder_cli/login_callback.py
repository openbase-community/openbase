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

import ipaddress
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx

logger = logging.getLogger(__name__)
REPLAY_TIMEOUT_SECONDS = 10.0

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
REDIRECT_QUERY_KEYS = ("redirect_uri", "redirect_url")
FORWARD_MIN_PORT = 1024
FORWARD_MAX_PORT = 65535
DEFAULT_FORWARD_TTL_SECONDS = 600
FORWARD_TOKEN_BYTES = 24
FORWARD_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)


def is_tailnet_forward_target(target: str) -> bool:
    try:
        address = ipaddress.ip_address(target)
    except ValueError:
        return False
    return "%" not in target and any(address in network for network in FORWARD_NETWORKS)


def loopback_callback_port(login_url: str) -> int | None:
    """The explicit loopback port named by ``login_url``'s redirect, if any.

    Returns None when the URL has no loopback redirect, the redirect has no
    explicit port, or the port is privileged (the phone cannot bind below
    1024 and neither can the workspace listener).
    """
    direct = _loopback_port(login_url)
    if direct is not None:
        return direct
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
    if parts.username is not None or parts.password is not None:
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
    relay_port: int | None = None
    expires_at: int | None = None

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
        payload = {
            "port": self.port,
            "target": self.target,
            "ttl_seconds": self.ttl_seconds,
            "token": self.token,
        }

        return payload

    def as_push_user_info(self) -> dict[str, str]:
        """The flat string keys carried by an APNs/FCM ``open_url`` push."""
        payload = {
            "forward_port": str(self.port),
            "forward_target": self.target,
            "forward_ttl_seconds": str(self.ttl_seconds),
            "forward_token": self.token,
        }

        return payload


def relay_capability(port: int, expires_at: int, nonce: str) -> str:
    """Versioned capability in the existing URL-safe token wire field.

    Routing metadata is not a secret. The random suffix grants access and is
    compared as part of the complete token by the workspace relay.
    """
    return f"OBR1_{port}_{expires_at}_{nonce}"


@dataclass(frozen=True)
class LoopbackReplayTarget:
    """A pasted-back callback address, reduced to what may be replayed."""

    port: int
    path: str  # path plus query, exactly as the browser requested it

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.path}"


def loopback_replay_target(pasted: str) -> LoopbackReplayTarget | None:
    """Parse a callback address the user pasted back from the phone.

    Only ``http://localhost|127.0.0.1|[::1]:<port>/...`` with an explicit
    unprivileged port qualifies; anything else returns None so a pasted
    address can never make the workspace fetch a non-loopback host. The
    fragment is dropped (browsers never send it).
    """
    candidate = pasted.strip()
    if any(ord(char) < 32 or char == "\x7f" for char in candidate):
        return None
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() != "http" or host is None:
        return None
    if host.lower() not in LOOPBACK_HOSTS or host.lower() == "0.0.0.0":
        return None
    if port is None or not FORWARD_MIN_PORT <= port <= FORWARD_MAX_PORT:
        return None
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return LoopbackReplayTarget(port=port, path=path)


def replay_loopback_callback(url: str) -> dict:
    """GET a pasted-back callback against this host's loopback; result only.

    Shared by ``openbase-coder browser replay`` and the local API endpoint,
    so it must stay free of Django imports: the command runs from any
    terminal, with no settings configured. Redirects are never followed and
    the single-use code in ``url`` is never logged.
    """
    target = loopback_replay_target(url)
    if target is None:
        raise ValueError("not a loopback callback address")
    try:
        response = httpx.get(
            target.url, follow_redirects=False, timeout=REPLAY_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        logger.info(
            "oauth callback replay to port %s failed: %s",
            target.port,
            type(exc).__name__,
        )
        return {
            "ok": False,
            "port": target.port,
            "error": f"localhost:{target.port} did not answer: {type(exc).__name__}",
        }
    return {
        "ok": response.status_code < 400,
        "port": target.port,
        "status_code": response.status_code,
    }
