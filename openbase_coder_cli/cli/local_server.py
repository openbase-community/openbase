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

DEFAULT_LOCAL_SERVER_URL = "http://127.0.0.1:7999"


class LocalInstallationAuth(httpx.Auth):
    """Use the existing host capability without depending on cloud reachability."""

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {get_local_api_token()}"
        yield request


def server_auth(url: str) -> httpx.Auth:
    destination = urlparse(url)
    if destination.scheme in {"http", "https"} and destination.hostname in {
        "127.0.0.1", "::1", "localhost",
    }:
        return LocalInstallationAuth()
    # Never send this installation's capability to a configured remote server.
    return CloudAccessTokenAuth(get_token_manager())


def local_server_url() -> str:
    return os.environ.get(
        "OPENBASE_CODER_CLI_SERVER_URL",
        os.environ.get("OPENBASE_CODER_CLI_LOCAL_SERVER_URL", DEFAULT_LOCAL_SERVER_URL),
    ).rstrip("/")


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
