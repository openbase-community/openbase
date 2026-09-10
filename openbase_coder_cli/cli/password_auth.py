"""Secret-safe password authentication for non-browser CLI environments."""

from __future__ import annotations

import click
import httpx


def exchange_password_for_jwts(
    *, web_backend_url: str, email: str, password: str
) -> tuple[str, str, int]:
    login_response = httpx.post(
        f"{web_backend_url}/_allauth/app/v1/auth/login",
        json={"email": email, "password": password},
        headers={"Accept": "application/json", "User-Agent": "openbase-coder-cli"},
        timeout=30,
    )
    login_response.raise_for_status()
    login_payload = login_response.json()
    meta = login_payload.get("meta") or {}
    if meta.get("is_authenticated") is not True:
        raise click.ClickException("Password login was not accepted.")

    data = login_payload.get("data") or {}
    access_token = data.get("access_token") or meta.get("access_token")
    refresh_token = data.get("refresh_token") or meta.get("refresh_token")
    expires_in = int(meta.get("access_token_expires_in") or 300)
    if access_token and refresh_token:
        return str(access_token), str(refresh_token), expires_in

    session_token = meta.get("session_token")
    if not session_token:
        raise click.ClickException(
            "Password login succeeded but returned no reusable session credential."
        )
    reissue_response = httpx.post(
        f"{web_backend_url}/api/openbase/auth/tokens/reissue/",
        headers={"X-Session-Token": str(session_token)},
        timeout=30,
    )
    reissue_response.raise_for_status()
    reissue_payload = reissue_response.json()
    access_token = reissue_payload.get("access_token", "")
    refresh_token = reissue_payload.get("refresh_token", "")
    expires_in = int(reissue_payload.get("access_token_expires_in") or 300)
    if not access_token or not refresh_token:
        raise click.ClickException(
            "Password login succeeded but token reissue returned no JWT pair."
        )
    return str(access_token), str(refresh_token), expires_in
