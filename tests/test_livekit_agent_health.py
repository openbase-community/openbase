"""Room creation waits for the livekit-agent worker (cold Workspace wake)."""

# ruff: noqa: E402 - Django must be configured before the views import.

from __future__ import annotations

import os
from types import SimpleNamespace

import httpx

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django
from rest_framework.test import APIRequestFactory, force_authenticate

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import (
    livekit as livekit_views,
)
from openbase_coder_cli.openbase_coder_cli_app import livekit_agent_health


def test_worker_ready_only_on_a_200_health_answer(monkeypatch):
    monkeypatch.setenv("LIVEKIT_AGENT_PORT", "18081")
    calls: list[str] = []

    class _Client:
        def __init__(self, *, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            calls.append(url)
            return httpx.Response(status_code=503)

    monkeypatch.setattr(livekit_agent_health.httpx, "Client", _Client)
    assert livekit_agent_health.livekit_agent_worker_ready() is False
    assert calls == ["http://127.0.0.1:18081/"]


def test_worker_unreachable_means_not_ready(monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(livekit_agent_health.httpx, "Client", boom)
    assert livekit_agent_health.livekit_agent_worker_ready() is False


def test_room_token_refuses_until_the_worker_is_ready(monkeypatch):
    monkeypatch.setattr(livekit_views, "livekit_agent_worker_ready", lambda: False)
    monkeypatch.setattr(
        livekit_views, "_livekit_client_token_credentials", lambda: ("key", "secret")
    )
    monkeypatch.setattr(
        livekit_views,
        "local_audio_readiness",
        lambda **_kwargs: SimpleNamespace(ready=True, detail=None),
    )
    monkeypatch.setattr(
        livekit_views,
        "ensure_openbase_cloud_audio_subscription",
        lambda **_kwargs: None,
    )
    request = APIRequestFactory().post(
        "/api/livekit-room-token/",
        data={"room_name": "room-1", "livekit_dispatch_agent_name": "livekit-agent"},
        format="json",
        HTTP_AUTHORIZATION="Bearer jwt.token.value",
    )
    user = SimpleNamespace(
        is_authenticated=True,
        email="caller@example.com",
        pk=1,
        get_full_name=lambda: "Caller",
    )
    force_authenticate(request, user=user, token={"email": "caller@example.com"})

    response = livekit_views.livekit_room_token(request)

    assert response.status_code == 503
    assert response.data["code"] == "agent_not_ready"
