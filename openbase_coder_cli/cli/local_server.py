from __future__ import annotations

import os

import click
import httpx

from openbase_coder_cli.config.token_manager import (
    CloudAccessTokenAuth,
    get_token_manager,
)

DEFAULT_LOCAL_SERVER_URL = "http://127.0.0.1:7999"


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
    **kwargs,
) -> httpx.Response:
    url = f"{local_server_url()}{path}"
    try:
        response = httpx.request(
            method,
            url,
            auth=CloudAccessTokenAuth(get_token_manager()),
            timeout=10,
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
