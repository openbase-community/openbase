"""``openbase-coder browser``: open web pages on the user's phone.

``browser open`` doubles as a ``BROWSER`` / ``GH_BROWSER`` handler in cloud
workspaces, where no local browser exists: CLIs that "open a browser" for a
login hand their URL to it, and it forwards the URL to the Openbase app on the
user's phone. As a handler it must never block or fail the calling CLI, so
any delivery problem degrades to printing the URL with paste-back guidance.

Delivery order: the app-control socket (the phone app is in front and has
this computer selected), then an Openbase Cloud push (the app is elsewhere,
backgrounded or closed; the user taps the notification), then the printed
URL with paste-back guidance.

When the login URL's ``redirect_uri`` points at ``localhost:<port>`` and this
host runs the embedded tailnet node (cloud workspaces), the command also
exposes that port on the tailnet for the duration of the login and asks the
phone to forward its own loopback port there, so the provider's redirect
reaches the CLI unchanged.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
from urllib.parse import urlsplit

import click

from openbase_coder_cli.cli.app_control import publish_open_url
from openbase_coder_cli.login_callback import (
    DEFAULT_FORWARD_TTL_SECONDS,
    LoopbackForward,
    loopback_callback_port,
)
from openbase_coder_cli.open_url_policy import open_url_error

logger = logging.getLogger(__name__)

OPENED_MESSAGE = "Sent to the Openbase app on your phone."
PUSHED_MESSAGE = "Sent a notification to your phone; tap it to open the page."
BROWSER_DELIVERY_TIMEOUT_SECONDS = 6.0
NOT_DELIVERED_HINT = (
    "Could not reach the Openbase app on your phone. Open this URL on any "
    "device; if the login ends on a localhost page that fails to load, paste "
    "that final address back here."
)
PUSH_TITLE = "Open a link from your workspace"


@click.group()
def browser() -> None:
    """Open web pages on your phone (usable as a BROWSER handler)."""


@browser.command("open")
@click.argument("url")
@click.option(
    "--no-forward",
    is_flag=True,
    help=(
        "Do not expose a localhost login callback port to the phone, even "
        "when the URL's redirect_uri names one."
    ),
)
@click.option(
    "--callback-port",
    type=click.IntRange(1024, 65535),
    default=None,
    help=(
        "Loopback port the login will redirect to, when it cannot be read "
        "from the URL's redirect_uri."
    ),
)
@click.option(
    "--no-push",
    is_flag=True,
    help="Do not fall back to an Openbase Cloud push notification.",
)
def browser_open(
    url: str, no_forward: bool, callback_port: int | None, no_push: bool
) -> None:
    """Open URL on your phone through the Openbase app.

    Always prints URL first, so it stays visible when the phone cannot be
    reached. Exits 0 unless URL itself is rejected (no scheme, or a data:,
    file: or javascript: URL).
    """
    error = open_url_error(url)
    if error:
        raise click.BadParameter(error, param_hint="URL")
    click.echo(url)

    forward: LoopbackForward | None = None
    if not no_forward:
        port = callback_port or loopback_callback_port(url)
        if port is not None:
            forward = _arrange_forward(port)

    if _deliver(url, forward):
        click.echo(OPENED_MESSAGE)
    elif not no_push and _push(url, forward):
        click.echo(PUSHED_MESSAGE)
    else:
        click.echo(NOT_DELIVERED_HINT)
    if forward is not None:
        click.echo(
            f"Your phone will forward localhost:{forward.port} back to this "
            f"workspace for {forward.ttl_seconds // 60} minutes."
        )


def _arrange_forward(port: int) -> LoopbackForward | None:
    """Expose ``port`` on this node's tailnet for the login, if possible.

    Only embedded-node hosts (cloud workspaces) can do this today; elsewhere
    the printed paste-back guidance is the fallback. Any failure is reported
    on stdout and never aborts the calling CLI.
    """
    from openbase_coder_cli.services import tailscale_provider
    from openbase_coder_cli.services.tunneld import (
        TunneldForwardError,
        tunneld_add_forward,
        tunneld_self_dns_name,
        tunneld_status,
    )

    if not tailscale_provider.is_netmesh_tsnet():
        click.echo(
            f"Login callback on localhost:{port} cannot be forwarded from "
            "this host; paste the final localhost address back here instead."
        )
        return None
    target = _self_tailnet_target(tunneld_status, tunneld_self_dns_name)
    if target is None:
        click.echo("The tailnet node is not up, so the callback cannot be forwarded.")
        return None
    try:
        tunneld_add_forward(
            port, ttl_seconds=DEFAULT_FORWARD_TTL_SECONDS, one_shot=True
        )
    except TunneldForwardError as exc:
        click.echo(f"Could not expose localhost:{port} on the tailnet: {exc}")
        return None
    return LoopbackForward.create(port, target)


def _self_tailnet_target(status_fn, dns_name_fn) -> str | None:
    """This node's IPv4 tailnet address (phones dial it without DNS), else
    its MagicDNS name."""
    _available, payload, _error = status_fn()
    self_info = payload.get("Self") if isinstance(payload, dict) else None
    if isinstance(self_info, dict):
        for raw in self_info.get("TailscaleIPs") or []:
            try:
                address = ipaddress.ip_address(str(raw))
            except ValueError:
                continue
            if address.version == 4:
                return str(address)
    return dns_name_fn()


def _deliver(url: str, forward: LoopbackForward | None) -> bool:
    """Bound the delivery attempt even if authentication or the server stalls."""
    delivered = []

    def attempt() -> None:
        delivered.append(_try_deliver(url, forward))

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(BROWSER_DELIVERY_TIMEOUT_SECONDS)
    return bool(delivered and delivered[0])


def _try_deliver(url: str, forward: LoopbackForward | None) -> bool:
    """Whether the phone app acknowledged receipt; any failure means no."""
    try:
        data = publish_open_url(
            url, loopback_forward=forward.as_app_control() if forward else None
        )
    except (click.ClickException, OSError, ValueError):
        # Server unreachable, rejected, or answered garbage: the printed URL
        # and hint are the fallback, and the calling CLI must keep going.
        return False
    return isinstance(data, dict) and data.get("delivered") is True


def _push(url: str, forward: LoopbackForward | None) -> bool:
    """Ask Openbase Cloud to notify the phone; False on any failure."""
    from openbase_coder_cli.config.cloud_notifications import send_notification_push

    user_info = {"openbase_destination": "open_url", "url": url}
    if forward is not None:
        user_info.update(forward.as_push_user_info())
    host = urlsplit(url).hostname or url
    done = []

    def attempt() -> None:
        try:
            send_notification_push(
                title=PUSH_TITLE, body=f"Tap to open {host}", user_info=user_info
            )
        except Exception as exc:  # noqa: BLE001 - handler must never fail the CLI
            logger.info("browser open: push fallback unavailable: %s", exc)
            done.append(False)
            return
        done.append(True)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(BROWSER_DELIVERY_TIMEOUT_SECONDS * 3)
    return bool(done and done[0])
