from __future__ import annotations

import os
from urllib.parse import urlparse

import click
import httpx

from openbase_coder_cli.config.local_api_token import get_local_api_token
from openbase_coder_cli.config.token_manager import (
    CloudAccessTokenAuth,
    get_token_manager,
)

DEFAULT_LOCAL_SERVER_HOST = "127.0.0.1"
DEFAULT_LOCAL_SERVER_PORT = 7999
DEFAULT_LOCAL_SERVER_URL = (
    f"http://{DEFAULT_LOCAL_SERVER_HOST}:{DEFAULT_LOCAL_SERVER_PORT}"
)


class LocalInstallationAuth(httpx.Auth):
    """Use the existing host capability without depending on cloud reachability."""

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {get_local_api_token()}"
        yield request


def server_auth(url: str) -> httpx.Auth:
    destination = urlparse(url)
    if destination.scheme in {"http", "https"} and destination.hostname in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        return LocalInstallationAuth()
    # Never send this installation's capability to a configured remote server.
    return CloudAccessTokenAuth(get_token_manager())


def local_server_url() -> str:
    """Where this installation's own coder server listens.

    An explicit URL wins; otherwise the host and port the server was started
    with (``OPENBASE_CODER_CLI_HOST`` / ``OPENBASE_CODER_CLI_PORT``, which
    container runtimes move off 7999 — Maritime serves on 18789). Falling
    back to the stock port there made every local probe a connection
    refused: the Cloud heartbeat never saw a live call, so a hosted
    workspace was idle-slept mid-call (field test 2026-10-08).
    """
    explicit = os.environ.get("OPENBASE_CODER_CLI_SERVER_URL") or os.environ.get(
        "OPENBASE_CODER_CLI_LOCAL_SERVER_URL"
    )
    if explicit:
        return explicit.rstrip("/")
    host = (
        os.environ.get("OPENBASE_CODER_CLI_HOST", "").strip()
        or DEFAULT_LOCAL_SERVER_HOST
    )
    port_raw = os.environ.get("OPENBASE_CODER_CLI_PORT", "").strip()
    try:
        port = int(port_raw) if port_raw else DEFAULT_LOCAL_SERVER_PORT
    except ValueError:
        port = DEFAULT_LOCAL_SERVER_PORT
    return f"http://{host}:{port}"


def local_server_request(
    method: str,
    path: str,
    *,
    ok_statuses: tuple[int, ...] = (),
    timeout: float = 10,
    **kwargs,
) -> httpx.Response:
    url = f"{local_server_url()}{path}"
    # A local redirect must not carry an installation capability off this host.
    kwargs["follow_redirects"] = False
    try:
        response = httpx.request(
            method,
            url,
            auth=server_auth(url),
            timeout=timeout,
            **kwargs,
        )
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"Unable to reach the local Openbase Coder server: {exc}"
        ) from None

    if response.status_code >= 400 and response.status_code not in ok_statuses:
        raise click.ClickException(response_error(response))
    return response


def response_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (
            response.text.strip()
            or f"Request failed with status {response.status_code}."
        )

    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error")
        if detail:
            return str(detail)
    return f"Request failed with status {response.status_code}."
