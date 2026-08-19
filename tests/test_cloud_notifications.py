from __future__ import annotations

import httpx
import pytest

from openbase_coder_cli.config import cloud_notifications
from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
)


class TokenManager:
    def get_access_token(self) -> str:
        return "jwt-token"


def test_send_user_say_fallback_uses_fixed_cloud_contract(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(cloud_notifications, "web_backend_url", lambda: "https://cloud.example")
    monkeypatch.setattr(
        cloud_notifications,
        "get_token_manager",
        lambda _url: TokenManager(),
    )
    monkeypatch.setattr(
        cloud_notifications.httpx,
        "post",
        lambda url, **kwargs: calls.append((url, kwargs))
        or httpx.Response(202, json={"message": "Notification accepted."}),
    )

    cloud_notifications.send_user_say_fallback(
        agent_name="Dottie",
        message="Review is ready",
        thread_id="thread-42",
    )

    assert calls[0][0] == (
        "https://cloud.example/api/openbase/notifications/user-say-fallback/"
    )
    assert calls[0][1]["headers"]["Authorization"] == "Bearer jwt-token"
    assert calls[0][1]["json"] == {
        "agent_name": "Dottie",
        "message": "Review is ready",
        "thread_id": "thread-42",
    }


def test_send_user_say_fallback_requires_cloud_login(monkeypatch) -> None:
    monkeypatch.setattr(cloud_notifications, "web_backend_url", lambda: "https://cloud.example")
    monkeypatch.setattr(
        cloud_notifications,
        "get_token_manager",
        lambda _url: TokenManager(),
    )
    monkeypatch.setattr(
        cloud_notifications.httpx,
        "post",
        lambda *_args, **_kwargs: httpx.Response(401),
    )

    with pytest.raises(AuthLoginRequiredError, match="login is required"):
        cloud_notifications.send_user_say_fallback(
            agent_name="Dottie",
            message="Review is ready",
            thread_id="thread-42",
        )


def test_send_user_say_fallback_preserves_retryable_network_failure(monkeypatch) -> None:
    monkeypatch.setattr(cloud_notifications, "web_backend_url", lambda: "https://cloud.example")
    monkeypatch.setattr(
        cloud_notifications,
        "get_token_manager",
        lambda _url: TokenManager(),
    )

    def fail(*_args, **_kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(cloud_notifications.httpx, "post", fail)

    with pytest.raises(AuthTransientError, match="offline"):
        cloud_notifications.send_user_say_fallback(
            agent_name="Dottie",
            message="Review is ready",
            thread_id="thread-42",
        )
