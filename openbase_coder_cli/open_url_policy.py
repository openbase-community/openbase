"""Which URLs Openbase will ask a phone to open.

Shared by the local app-control API (server side) and the
``openbase-coder browser open`` command (client side), so both enforce one
policy. Dependency-free on purpose: the browser command runs as a ``BROWSER``
handler and must start fast without loading Django.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

DISALLOWED_URL_SCHEMES = {"data", "file", "javascript"}
URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")


def open_url_error(value: str) -> str | None:
    """Return why ``value`` may not be opened on a phone, or None if it may."""
    try:
        scheme = urlparse(value).scheme
    except ValueError:
        return "url is malformed."
    if not scheme:
        return "url must include a scheme."
    if not URL_SCHEME_RE.match(scheme):
        return "url has an invalid scheme."
    if scheme.lower() in DISALLOWED_URL_SCHEMES:
        return f"{scheme} URLs are not allowed."
    if any(ord(char) < 32 for char in value):
        return "url must not contain control characters."
    return None


def normalize_open_url(value: str) -> str:
    """Lowercase the scheme of an allowed URL.

    Schemes are case-insensitive (RFC 3986), but Android resolves intents
    case-sensitively, so "Https://..." (an autocapitalized URL) would match
    no browser there.
    """
    scheme, separator, rest = value.partition(":")
    return f"{scheme.lower()}{separator}{rest}" if separator else value
