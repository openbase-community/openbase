"""``openbase-coder browser``: open web pages on the user's phone.

``browser open`` doubles as a ``BROWSER`` / ``GH_BROWSER`` handler in cloud
workspaces, where no local browser exists: CLIs that "open a browser" for a
login hand their URL to it, and it forwards the URL to the Openbase app on the
user's phone. As a handler it must never block or fail the calling CLI, so
any delivery problem degrades to printing the URL with paste-back guidance.
"""

from __future__ import annotations

import threading

import click

from openbase_coder_cli.cli.app_control import publish_open_url
from openbase_coder_cli.open_url_policy import open_url_error

OPENED_MESSAGE = "Sent to the Openbase app on your phone."
BROWSER_DELIVERY_TIMEOUT_SECONDS = 6.0
NOT_DELIVERED_HINT = (
    "Could not reach the Openbase app on your phone. Open this URL on any "
    "device; if the login ends on a localhost page that fails to load, paste "
    "that final address back here."
)


@click.group()
def browser() -> None:
    """Open web pages on your phone (usable as a BROWSER handler)."""


@browser.command("open")
@click.argument("url")
@click.option(
    "--no-forward",
    is_flag=True,
    help=(
        "Do not forward a localhost login callback to the phone. Currently a "
        "no-op: callback forwarding is not available yet, so nothing is "
        "forwarded either way."
    ),
)
def browser_open(url: str, no_forward: bool) -> None:
    """Open URL on your phone through the Openbase app.

    Always prints URL first, so it stays visible when the phone cannot be
    reached. Exits 0 unless URL itself is rejected (no scheme, or a data:,
    file: or javascript: URL).
    """
    del no_forward  # Accepted for forward compatibility; see --help.
    error = open_url_error(url)
    if error:
        raise click.BadParameter(error, param_hint="URL")
    click.echo(url)
    if _deliver(url):
        click.echo(OPENED_MESSAGE)
    else:
        click.echo(NOT_DELIVERED_HINT)


def _deliver(url: str) -> bool:
    """Bound the delivery attempt even if authentication or the server stalls."""
    delivered = []

    def attempt() -> None:
        delivered.append(_try_deliver(url))

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(BROWSER_DELIVERY_TIMEOUT_SECONDS)
    return bool(delivered and delivered[0])


def _try_deliver(url: str) -> bool:
    """Whether the phone app acknowledged receipt; any failure means no."""
    try:
        data = publish_open_url(url)
    except (click.ClickException, OSError, ValueError):
        # Server unreachable, rejected, or answered garbage: the printed URL
        # and hint are the fallback, and the calling CLI must keep going.
        return False
    return isinstance(data, dict) and data.get("delivered") is True
