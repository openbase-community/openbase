"""Authenticated Openbase Cloud client for explicit inbound call rings."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
    get_token_manager,
)
from openbase_coder_cli.services.onboarding import web_backend_url

INBOUND_VOICE_INVITATIONS_PATH = "/api/openbase/voice/invitations/"
REQUEST_TIMEOUT_SECONDS = 15


class InboundCallCloudError(RuntimeError):
    """Openbase Cloud rejected an inbound call invitation."""


@dataclass(frozen=True, slots=True)
class CloudInboundCallAcceptance:
    invitation_id: str
    expires_at: int
    device_count: int


def request_inbound_call_ring(
    *,
    invitation_id: str,
    caller_name: str,
    access_token: str | None = None,
) -> CloudInboundCallAcceptance:
    backend_url = web_backend_url()
    token = access_token or get_token_manager(backend_url).get_access_token()
    try:
        response = httpx.post(
            f"{backend_url}{INBOUND_VOICE_INVITATIONS_PATH}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            json={
                "invitation_id": invitation_id,
                "caller_name": caller_name,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise AuthTransientError(f"Cloud call request failed: {exc}") from exc

    if response.status_code == 401:
        raise AuthLoginRequiredError(
            "Openbase Cloud login is required to ring your phone."
        )
    if response.status_code >= 500:
        raise AuthTransientError(
            "Cloud call request failed with backend status "
            f"{response.status_code}."
        )
    if response.status_code != 202:
        raise InboundCallCloudError(_response_detail(response))

    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthTransientError("Cloud call request returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise AuthTransientError("Cloud call request returned an invalid payload.")
    returned_id = payload.get("invitation_id")
    expires_at = payload.get("expires_at")
    device_count = payload.get("device_count")
    if (
        returned_id != invitation_id
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or not isinstance(device_count, int)
        or isinstance(device_count, bool)
        or device_count < 1
    ):
        raise AuthTransientError("Cloud call request returned an invalid payload.")
    return CloudInboundCallAcceptance(
        invitation_id=returned_id,
        expires_at=expires_at,
        device_count=device_count,
    )


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or (
            f"Cloud call request failed with status {response.status_code}."
        )
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        field_errors = [
            f"{field}: {message}"
            for field, errors in payload.items()
            for message in (errors if isinstance(errors, list) else [errors])
            if isinstance(message, str) and message
        ]
        if field_errors:
            return "; ".join(field_errors)
    return f"Cloud call request failed with status {response.status_code}."
