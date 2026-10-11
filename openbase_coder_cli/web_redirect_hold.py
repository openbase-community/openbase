"""Hold completion alerts while the phone is showing a page we sent it to.

When this computer sends the user's phone to a web page (``browser open``,
``user phone open-url``, a localhost login bridge), a "thread finished" or
"new report" banner arriving moments later pulls the user out of the
sign-in or page they were sent to. For ``HOLD_SECONDS`` after each such
redirect, informational feed notifications are created with an
``alert_after`` time: the feed entry (in-app list, unread badge) exists
immediately, but the phone apps and the Cloud push wait until then to
alert, and skip the alert if the item was read in the meantime.

The CLI command that redirects and the API server that produces the
notifications are separate processes, so the redirect time lives in a small
file in the data directory.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from openbase_coder_cli.cli.utils import get_data_dir

HOLD_SECONDS = 5 * 60
REDIRECT_FILE = "web_redirect.json"


def record_web_redirect(now: float | None = None) -> None:
    """Note that the phone was just sent to a web page."""
    path = _redirect_path()
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as tmp:
        json.dump({"redirected_at": time.time() if now is None else now}, tmp)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def alert_hold_until(now: float | None = None) -> float | None:
    """Epoch seconds until which alerts are held, or None when not held."""
    try:
        payload = json.loads(_redirect_path().read_text(encoding="utf-8"))
        redirected_at = float(payload["redirected_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    current = time.time() if now is None else now
    until = redirected_at + HOLD_SECONDS
    # A redirect "in the future" is clock skew or a corrupt file, not a hold.
    if redirected_at > current + 60 or until <= current:
        return None
    return until


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _redirect_path() -> Path:
    return get_data_dir() / REDIRECT_FILE
