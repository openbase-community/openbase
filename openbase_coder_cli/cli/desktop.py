from __future__ import annotations

import platform
from typing import Any

import click

from openbase_coder_cli import desktop_control
from openbase_coder_cli.cli.local_server import local_server_request

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
    try:
        return desktop_control.request(method, path, json=json, launch=launch)
    except desktop_control.DesktopControlError as exc:
        raise click.ClickException(str(exc)) from None
