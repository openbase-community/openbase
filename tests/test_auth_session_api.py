"""Tests for the local /api/auth/session/ validated login report."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import auth as auth_views  # noqa: E402


def _get_session(monkeypatch, status_dict):
    monkeypatch.setattr(
        auth_views,
        "get_token_manager",
        lambda: SimpleNamespace(login_status=lambda: status_dict),
    )
    request = APIRequestFactory().get("/api/auth/session/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return auth_views.auth_session(request)


def test_auth_session_reports_expired_login_as_logged_out(monkeypatch):
    response = _get_session(
        monkeypatch,
        {"status": "login_expired", "validated": True, "detail": "rejected"},
    )

    assert response.status_code == 200
    assert response.data["logged_in"] is False
    assert response.data["status"] == "login_expired"
    assert response.data["detail"] == "rejected"


def test_auth_session_reports_validated_login(monkeypatch):
    response = _get_session(
        monkeypatch, {"status": "logged_in", "validated": True, "detail": ""}
    )

    assert response.data["logged_in"] is True
    assert response.data["status"] == "logged_in"
    assert response.data["validated"] is True
