"""Regression tests for the installation-scoped local API trust boundary."""

# ruff: noqa: E402 -- Django must be configured before app imports.

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from django.test import override_settings  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

django.setup()

from openbase_coder_cli.config import authentication  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import auth as auth_views  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import (
    onboarding as onboarding_views,  # noqa: E402
)
from openbase_coder_cli.openbase_coder_cli_app import plugins_tools  # noqa: E402

CAPABILITY = "local-capability-with-at-least-forty-characters-123"


def _client(*, remote_addr: str, forwarded_for: str | None = None) -> APIClient:
    defaults = {"REMOTE_ADDR": remote_addr}
    if forwarded_for:
        defaults["HTTP_X_FORWARDED_FOR"] = forwarded_for
        defaults["HTTP_X_FORWARDED_HOST"] = "127.0.0.1"
        defaults["HTTP_X_FORWARDED_PROTO"] = "http"
    return APIClient(**defaults)


@override_settings(ALLOWED_HOSTS=["testserver", "127.0.0.1", ".ts.net"])
def test_removed_refresh_endpoint_never_returns_owner_token(monkeypatch):
    calls = []
    monkeypatch.setattr(
        auth_views,
        "get_token_manager",
        lambda: SimpleNamespace(
            has_refresh_token=True,
            get_access_token_payload=lambda: calls.append("minted"),
        ),
    )

    requests = [
        _client(remote_addr="100.64.0.20"),
        _client(remote_addr="127.0.0.1"),
        _client(remote_addr="127.0.0.1", forwarded_for="100.64.0.20"),
    ]
    for client in requests:
        response = client.post("/api/auth/refresh-jwt/", format="json")
        assert response.status_code == 410
    assert calls == []


@override_settings(ALLOWED_HOSTS=["testserver", "127.0.0.1", ".ts.net"])
def test_loopback_and_proxy_headers_do_not_replace_capability(monkeypatch):
    monkeypatch.setattr(authentication, "get_local_api_token", lambda: CAPABILITY)
    monkeypatch.setattr(
        auth_views,
        "get_token_manager",
        lambda: SimpleNamespace(login_status=lambda: {"status": "logged_out"}),
    )

    for client in (
        _client(remote_addr="100.64.0.20"),
        _client(remote_addr="127.0.0.1"),
        _client(remote_addr="127.0.0.1", forwarded_for="100.64.0.20"),
    ):
        response = client.get("/api/auth/session/")
        assert response.status_code in {401, 403}


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_installation_capability_authorizes_intended_local_api_path(monkeypatch):
    monkeypatch.setattr(authentication, "get_local_api_token", lambda: CAPABILITY)
    monkeypatch.setattr(
        auth_views,
        "get_token_manager",
        lambda: SimpleNamespace(
            login_status=lambda: {
                "status": "logged_in",
                "validated": True,
                "detail": "",
            }
        ),
    )
    client = _client(remote_addr="100.64.0.20")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {CAPABILITY}")

    response = client.get("/api/auth/session/")

    assert response.status_code == 200
    assert response.json() == {
        "logged_in": True,
        "status": "logged_in",
        "validated": True,
        "detail": "",
    }


@override_settings(ALLOWED_HOSTS=["testserver", ".ts.net"])
def test_adjacent_owner_state_and_logout_are_not_anonymous(monkeypatch):
    calls = []
    monkeypatch.setattr(
        auth_views,
        "get_token_manager",
        lambda: SimpleNamespace(
            clear=lambda: calls.append("logout"),
            login_status=lambda: calls.append("session"),
        ),
    )
    monkeypatch.setattr(
        onboarding_views,
        "onboarding_status_payload",
        lambda: calls.append("onboarding"),
    )
    monkeypatch.setattr(
        plugins_tools,
        "get_console_registry_payload",
        lambda: calls.append("plugins"),
    )
    client = _client(remote_addr="100.64.0.20")

    responses = [
        client.get("/api/auth/session/"),
        client.post("/api/auth/logout/", format="json"),
        client.get("/api/onboarding/status/"),
        client.get("/api/onboarding/cloud-state/"),
        client.get("/api/plugins/console-registry/"),
    ]

    assert all(response.status_code in {401, 403} for response in responses)
    assert calls == []


def test_local_capability_file_is_rotatable_and_owner_only(tmp_path):
    from openbase_coder_cli.config.local_api_token import (
        get_local_api_token,
        rotate_local_api_token,
    )

    path = tmp_path / "local-api-token"
    first = get_local_api_token(path)
    second = rotate_local_api_token(path)

    assert first != second
    assert path.read_text(encoding="utf-8").strip() == second
    assert path.stat().st_mode & 0o777 == 0o600
