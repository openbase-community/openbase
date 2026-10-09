"""Client side of the local app-control API (commands for the phone app)."""

from __future__ import annotations

from openbase_coder_cli.cli.local_server import local_server_request

APP_CONTROL_PATH = "/api/user/ios-app-control/"


def publish_app_control(payload: dict[str, object], *, timeout: float = 10) -> dict:
    """Publish one app-control command; the result reports ``delivered``."""
    response = local_server_request(
        "POST", APP_CONTROL_PATH, json=payload, timeout=timeout
    )
    return response.json()


def publish_open_url(url: str) -> dict:
    """Ask the connected Openbase phone app to open ``url``."""
    return publish_app_control({"action": "open_url", "url": url})
