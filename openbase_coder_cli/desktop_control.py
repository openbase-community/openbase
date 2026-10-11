"""Client for the Openbase desktop app's local control server (macOS).

The Electron app publishes a loopback port and per-run secret in
``~/.openbase/desktop-control.json``. Both the ``openbase-coder desktop`` CLI
and the local API (phone-initiated screen share) use this client, so it raises
plain :class:`DesktopControlError` rather than Click exceptions.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import httpx

from openbase_coder_cli.paths import DESKTOP_CONTROL_JSON_PATH

DESKTOP_APP_NAMES = ("Openbase", "Openbase Coder")
DESKTOP_UNREACHABLE_MESSAGE = (
    "Unable to reach the Openbase Coder desktop app. Open Openbase.app "
    "(or legacy Openbase Coder.app) and try again."
)


class DesktopControlError(RuntimeError):
    """A desktop control request failed.

    ``code`` carries the app's machine-readable reason when it sent one (for
    example ``screen_recording_permission_required``); ``unreachable`` is
    True when the app itself could not be reached or launched.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        unreachable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.unreachable = unreachable


def read_control_file() -> dict[str, Any]:
    try:
        payload = json.loads(DESKTOP_CONTROL_JSON_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DesktopControlError(
            "Openbase Coder desktop control file was not found.", unreachable=True
        ) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise DesktopControlError(
            f"Openbase Coder desktop control file is invalid: {exc}", unreachable=True
        ) from None

    port = payload.get("port")
    secret = payload.get("secret")
    if (
        not isinstance(port, int)
        or port <= 0
        or not isinstance(secret, str)
        or not secret
    ):
        raise DesktopControlError(
            "Openbase Coder desktop control file is incomplete.", unreachable=True
        )
    return {"port": port, "secret": secret}


def request_once(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    timeout: float = 15,
) -> dict[str, Any]:
    control = read_control_file()
    url = f"http://127.0.0.1:{control['port']}{path}"
    headers = {"X-Openbase-Desktop-Secret": control["secret"]}
    try:
        response = httpx.request(
            method, url, headers=headers, json=json, timeout=timeout
        )
    except httpx.HTTPError as exc:
        raise DesktopControlError(
            f"Desktop control request failed: {exc}", unreachable=True
        ) from None

    payload: Any = {}
    if response.content:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if response.status_code >= 400:
        message = (
            payload.get("error")
            or payload.get("detail")
            or f"Desktop control request failed with status {response.status_code}."
        )
        code = payload.get("code") if isinstance(payload.get("code"), str) else None
        raise DesktopControlError(str(message), code=code)
    return payload


def launch_desktop_app() -> None:
    for app_name in DESKTOP_APP_NAMES:
        result = subprocess.run(
            ["open", "-a", app_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
    raise DesktopControlError(
        "Unable to launch Openbase.app or legacy Openbase Coder.app.",
        unreachable=True,
    )


def wait_for_control_file(timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            read_control_file()
            return
        except DesktopControlError:
            time.sleep(0.25)


def request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    launch: bool = True,
    timeout: float = 15,
) -> dict[str, Any]:
    """Send a control request, launching the desktop app once if unreachable.

    Errors the app itself reports (it answered, but refused) are raised
    immediately; only an unreachable app triggers the launch-and-retry.
    """
    last_error: str | None = None
    for attempt in range(2 if launch else 1):
        try:
            return request_once(method, path, json=json, timeout=timeout)
        except DesktopControlError as exc:
            if not exc.unreachable:
                raise
            last_error = str(exc)
            if not launch or attempt > 0:
                break
            launch_desktop_app()
            wait_for_control_file()

    raise DesktopControlError(
        DESKTOP_UNREACHABLE_MESSAGE
        + (f" Last error: {last_error}" if last_error else ""),
        unreachable=True,
    )
