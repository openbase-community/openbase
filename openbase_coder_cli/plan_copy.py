"""The one call to action for a low or used-up Openbase Cloud allowance.

Gabe's exact wording (2026-10-11). Every written allowance, credit and plan
message uses it; the iOS, Android and web clients carry the same line. The
spoken form lives with the voice agent (``SUBSCRIBE_SPOKEN``).
"""

from urllib.parse import urlparse

PRODUCTION_HOST = "app.openbase.cloud"


def subscribe_pro_line(web_backend_url: str | None = None) -> str:
    """The line for the Cloud the user is on: staging names its own host."""
    host = urlparse(web_backend_url or "").netloc or PRODUCTION_HOST
    return f"Subscribe to a Pro plan on {host} for expanded access."


SUBSCRIBE_PRO_LINE = subscribe_pro_line()
