from __future__ import annotations

import json
import platform
import subprocess
import time
from typing import Any

import click
import httpx

from openbase_coder_cli.cli.local_server import local_server_request, response_error
from openbase_coder_cli.paths import DESKTOP_CONTROL_JSON_PATH

DESKTOP_APP_NAMES = ("Openbase", "Openbase Coder")
DESKTOP_NO_LAUNCH_HELP = (
    "Do not launch Openbase.app (or legacy Openbase Coder.app) if the desktop "
    "control server is not reachable."
)


@click.group("desktop")
def desktop() -> None:
    """Control the Openbase Coder desktop app."""


@desktop.group("screen-share")
def screen_share() -> None:
    """Start and stop the desktop LiveKit screen-share companion."""


@screen_share.command("start")
@click.option(
    "--room",
    "room_name",
    default="",
    help="Explicit LiveKit room name. Defaults to the latest active voice room.",
)
@click.option(
    "--no-launch",
    is_flag=True,
    help=DESKTOP_NO_LAUNCH_HELP,
)
def screen_share_start(room_name: str, no_launch: bool) -> None:
    """Start sharing the desktop app's screen to the active LiveKit room."""
    _require_macos()
    session = _load_companion_session(room_name)
    response = _desktop_control_request(
        "POST",
        "/livekit-companion/start-screen-share",
        json=session,
        launch=not no_launch,
    )
    click.echo(f"Desktop screen share started ({response.get('state') or 'sharing'}).")


@screen_share.command("stop")
@click.option(
    "--no-launch",
    is_flag=True,
    help=DESKTOP_NO_LAUNCH_HELP,
)
def screen_share_stop(no_launch: bool) -> None:
    """Stop the desktop app's LiveKit screen share."""
    _require_macos()
    response = _desktop_control_request(
        "POST",
        "/livekit-companion/stop-screen-share",
        json={},
        launch=not no_launch,
    )
    click.echo(f"Desktop screen share stopped ({response.get('state') or 'off'}).")


@screen_share.command("status")
@click.option(
    "--no-launch",
    is_flag=True,
    help=DESKTOP_NO_LAUNCH_HELP,
)
def screen_share_status(no_launch: bool) -> None:
    """Show the desktop app's screen-share companion status."""
    _require_macos()
    response = _desktop_control_request("GET", "/status", launch=not no_launch)
    companion = response.get("companion") if isinstance(response, dict) else None
    if not isinstance(companion, dict):
        click.echo("Desktop companion state: unknown")
        return
    state = companion.get("state") or "unknown"
    click.echo(f"Desktop companion state: {state}")
    if state != "off" and (error := companion.get("error")):
        click.echo(f"Error: {error}")


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise click.ClickException(
            "The desktop screen-share command controls the macOS Electron app. "
            "On Linux, use `openbase-coder computer-use screen-share start`."
        )


def _load_companion_session(room_name: str) -> dict[str, Any]:
    params = {"room_name": room_name.strip()} if room_name.strip() else None
    response = local_server_request(
        "GET",
        "/api/livekit-companion-session/",
        params=params,
    )
    payload = response.json()
    room_url = payload.get("roomUrl")
    companion_token = payload.get("companionToken")
    if not room_url or not companion_token:
        raise click.ClickException(
            "Companion session response is missing roomUrl or companionToken."
        )
    return payload


def _desktop_control_request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    launch: bool = True,
) -> dict[str, Any]:
    last_error: str | None = None
    for attempt in range(2 if launch else 1):
        try:
            return _desktop_control_request_once(method, path, json=json)
        except click.ClickException as exc:
            last_error = str(exc)
            if not launch or attempt > 0:
                break
            _launch_desktop_app()
            _wait_for_control_file()

    raise click.ClickException(
        "Unable to reach the Openbase Coder desktop app. Open Openbase.app "
        "(or legacy Openbase Coder.app) and try again."
        + (f" Last error: {last_error}" if last_error else "")
    )


def _desktop_control_request_once(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    control = _read_control_file()
    url = f"http://127.0.0.1:{control['port']}{path}"
    headers = {"X-Openbase-Desktop-Secret": control["secret"]}
    try:
        response = httpx.request(method, url, headers=headers, json=json, timeout=15)
    except httpx.HTTPError as exc:
        raise click.ClickException(f"Desktop control request failed: {exc}") from None

    if response.status_code >= 400:
        raise click.ClickException(response_error(response))
    if not response.content:
        return {}
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _read_control_file() -> dict[str, Any]:
    try:
        payload = json.loads(DESKTOP_CONTROL_JSON_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise click.ClickException(
            "Openbase Coder desktop control file was not found."
        ) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(
            f"Openbase Coder desktop control file is invalid: {exc}"
        ) from None

    port = payload.get("port")
    secret = payload.get("secret")
    if (
        not isinstance(port, int)
        or port <= 0
        or not isinstance(secret, str)
        or not secret
    ):
        raise click.ClickException("Openbase Coder desktop control file is incomplete.")
    return {"port": port, "secret": secret}


def _launch_desktop_app() -> None:
    for app_name in DESKTOP_APP_NAMES:
        result = subprocess.run(
            ["open", "-a", app_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
    raise click.ClickException(
        "Unable to launch Openbase.app or legacy Openbase Coder.app."
    )


def _wait_for_control_file() -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            _read_control_file()
            return
        except click.ClickException:
            time.sleep(0.25)
