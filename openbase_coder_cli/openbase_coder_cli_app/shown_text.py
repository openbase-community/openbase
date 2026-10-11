"""Show text on a phone whose Openbase app is not connected right now.

`show_text` normally reaches the app over its app-control websocket. A
suspended app has no socket, so the text is parked here (in memory, one
fetch, ten minutes at most) and the phone gets a Cloud push carrying only an
``openbase-app://show-text?id=<id>&host=<this computer's VPN device name>`` link.
Tapping it makes the app fetch the text from this computer over the VPN, so
the text itself never passes through Openbase Cloud or the push services.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from urllib.parse import urlencode

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

logger = logging.getLogger(__name__)
PARK_TTL_SECONDS = 10 * 60
MAX_PARKED = 16
SHOW_TEXT_LINK = "openbase-app://show-text"

_lock = threading.Lock()
_parked: dict[str, tuple[float, dict]] = {}


def park(text: str, label: str | None, url: str | None) -> str:
    """Keep the text for one fetch; returns its unguessable id."""
    now = time.monotonic()
    with _lock:
        for key, (expires, _item) in list(_parked.items()):
            if expires <= now:
                _parked.pop(key, None)
        while len(_parked) >= MAX_PARKED:
            _parked.pop(next(iter(_parked)))
        text_id = secrets.token_urlsafe(18)
        _parked[text_id] = (
            now + PARK_TTL_SECONDS,
            {"text": text, "label": label, "url": url},
        )
    return text_id


def take(text_id: str) -> dict | None:
    with _lock:
        entry = _parked.pop(text_id, None)
    if entry is None or entry[0] <= time.monotonic():
        return None
    return entry[1]


def push_parked_text(text: str, label: str | None, url: str | None) -> bool:
    """Park the text and push a tap-to-show link to the owner's phones."""
    from openbase_coder_cli.cli.browser import self_vpn_hostname
    from openbase_coder_cli.config.cloud_notifications import send_notification_push

    host = self_vpn_hostname()
    if host is None:
        return False
    text_id = park(text, label, url)
    link = f"{SHOW_TEXT_LINK}?{urlencode({'id': text_id, 'host': host})}"
    try:
        devices = send_notification_push(
            title=f"Your {label}" if label else "Text from your computer",
            body="Tap to see it in large type and copy it.",
            user_info={"openbase_destination": "open_url", "url": link},
        )
    except Exception as exc:  # any push failure: the caller falls back to the chat
        logger.info("show_text push unavailable: %s", type(exc).__name__)
        take(text_id)
        return False
    return devices != 0


@api_view(["GET"])
def shown_text_fetch(request, text_id: str):
    """The phone's one fetch of parked text after a show-text tap."""
    item = take(text_id)
    if item is None:
        return Response(
            {"detail": "This text has already been shown or has expired."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(item)
