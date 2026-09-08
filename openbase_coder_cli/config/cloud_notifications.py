"""Authenticated Openbase Cloud delivery for failed ``user say`` messages."""

from __future__ import annotations

import httpx

from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
    get_token_manager,
)
from openbase_coder_cli.services.onboarding import web_backend_url

USER_SAY_FALLBACK_PATH = "/api/openbase/notifications/user-say-fallback/"
REQUEST_TIMEOUT_SECONDS = 15
MAX_NOTIFICATION_AGENT_NAME_LENGTH = 80
MAX_NOTIFICATION_MESSAGE_LENGTH = 500


class UserSayNotificationError(RuntimeError):
    """Cloud rejected a user-say fallback notification."""


def send_user_say_fallback(
    *,
    agent_name: str,
    message: str,
    thread_id: str,
) -> None:
    backend_url = web_backend_url()
    token = get_token_manager(backend_url).get_access_token()
    notification_agent_name = _truncate_for_notification(
        agent_name,
        MAX_NOTIFICATION_AGENT_NAME_LENGTH,
    )
    notification_message = _truncate_for_notification(
        message,
        MAX_NOTIFICATION_MESSAGE_LENGTH,
    )
    try:
        response = httpx.post(
            f"{backend_url}{USER_SAY_FALLBACK_PATH}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            json={
                "agent_name": notification_agent_name,
                "message": notification_message,
                "thread_id": thread_id,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise AuthTransientError(f"Cloud notification request failed: {exc}") from exc

    if response.status_code == 401:
        raise AuthLoginRequiredError(
            "Openbase Cloud login is required to notify your phone."
        )
    if response.status_code >= 500:
        raise AuthTransientError(
            "Cloud notification request failed with backend status "
            f"{response.status_code}."
        )
    if response.status_code != 202:
        raise UserSayNotificationError(_response_detail(response))


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or (
            f"Cloud notification request failed with status {response.status_code}."
        )
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error")
        if detail:
            return str(detail)
        field_errors = [
            f"{field}: {message}"
            for field, errors in payload.items()
            for message in (errors if isinstance(errors, list) else [errors])
            if isinstance(message, str) and message
        ]
        if field_errors:
            return "; ".join(field_errors)
    return f"Cloud notification request failed with status {response.status_code}."


def _truncate_for_notification(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
