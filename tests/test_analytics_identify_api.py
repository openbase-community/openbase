"""Tests for the local analytics identify proxy used by the desktop app."""

# ruff: noqa: E402 -- Django must be configured before app imports.

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django
import httpx
import pytest
from asgiref.sync import async_to_sync
from rest_framework.test import APIRequestFactory, force_authenticate

django.setup()

from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
)
from openbase_coder_cli.openbase_coder_cli_app import analytics_identity, views


def _call(path: str, body: object):
    request = APIRequestFactory().post(path, body, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return async_to_sync(views.analytics_identify)(request)


def _stub_cloud(monkeypatch, *, status_code=200, body=None, token="cloud-token"):
    calls: list[dict] = []

    def fake_post(url, *, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return httpx.Response(status_code, json=body if body is not None else {})

    monkeypatch.setattr(analytics_identity.httpx, "post", fake_post)
    monkeypatch.setattr(
        analytics_identity,
        "get_token_manager",
        lambda backend_url: SimpleNamespace(get_access_token=lambda: token),
    )
    monkeypatch.setattr(
        analytics_identity, "web_backend_url", lambda: "https://cloud.example"
    )
    return calls


def test_identify_payload_keeps_only_cloud_fields() -> None:
    payload = analytics_identity.identify_payload(
        {
            "amplitude_device_id": "  abc-123  ",
            "ga_client_id": "GA1.2.3",
            "attribution_source": "producthunt",
            "email": "private@example.com",
            "prompt": "not an identifier",
            "attribution_campaign": 42,
            "attribution_medium": "",
        }
    )
    assert payload == {
        "amplitude_device_id": "abc-123",
        "ga_client_id": "GA1.2.3",
        "attribution_source": "producthunt",
    }


def test_identify_payload_truncates_and_rejects_non_dicts() -> None:
    long_id = "x" * 400
    assert analytics_identity.identify_payload({"ga_client_id": long_id}) == {
        "ga_client_id": "x" * 255
    }
    assert analytics_identity.identify_payload(["amplitude_device_id"]) == {}
    assert analytics_identity.identify_payload(None) == {}


@pytest.mark.parametrize(
    "path",
    ["/api/openbase/analytics/identify", "/api/openbase/analytics/identify/"],
)
def test_identify_relays_to_cloud_with_cli_token(monkeypatch, path) -> None:
    calls = _stub_cloud(
        monkeypatch,
        body={
            "analytics_key": "key-123",
            "message": "Linked 1 analytics identifier(s).",
        },
    )

    response = _call(path, {"amplitude_device_id": "device-1", "secret": "nope"})

    assert response.status_code == 200
    assert response.data == {
        "analytics_key": "key-123",
        "message": "Linked 1 analytics identifier(s).",
    }
    assert calls == [
        {
            "url": "https://cloud.example/api/openbase/analytics/identify/",
            "headers": {
                "Authorization": "Bearer cloud-token",
                "Accept": "application/json",
            },
            "json": {"amplitude_device_id": "device-1"},
            "timeout": analytics_identity.REQUEST_TIMEOUT_SECONDS,
        }
    ]


def test_identify_routes_resolve_both_slash_forms() -> None:
    from django.urls import resolve

    for path in (
        "/api/openbase/analytics/identify",
        "/api/openbase/analytics/identify/",
    ):
        assert resolve(path).func is views.analytics_identify


def test_identify_rejects_empty_payload(monkeypatch) -> None:
    calls = _stub_cloud(monkeypatch)

    response = _call("/api/openbase/analytics/identify", {"email": "x@example.com"})

    assert response.status_code == 400
    assert calls == []


def test_identify_maps_login_required_to_401(monkeypatch) -> None:
    _stub_cloud(monkeypatch)

    def raise_login():
        raise AuthLoginRequiredError("Log in to Openbase Cloud.")

    monkeypatch.setattr(
        analytics_identity,
        "get_token_manager",
        lambda backend_url: SimpleNamespace(get_access_token=raise_login),
    )

    response = _call("/api/openbase/analytics/identify", {"ga_client_id": "GA1.1.1"})

    assert response.status_code == 401
    assert response.data == {"error": "Log in to Openbase Cloud."}


def test_identify_maps_cloud_401_to_401(monkeypatch) -> None:
    _stub_cloud(monkeypatch, status_code=401, body={"detail": "bad token"})

    response = _call("/api/openbase/analytics/identify", {"ga_client_id": "GA1.1.1"})

    assert response.status_code == 401


@pytest.mark.parametrize("status_code", [400, 500])
def test_identify_maps_cloud_failures_to_502(monkeypatch, status_code) -> None:
    _stub_cloud(monkeypatch, status_code=status_code, body={"detail": "no"})

    response = _call("/api/openbase/analytics/identify", {"ga_client_id": "GA1.1.1"})

    assert response.status_code == 502
    assert str(status_code) in response.data["error"]


def test_identify_maps_transport_errors_to_502(monkeypatch) -> None:
    _stub_cloud(monkeypatch)

    def fail_post(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(analytics_identity.httpx, "post", fail_post)
    response = _call("/api/openbase/analytics/identify", {"ga_client_id": "GA1.1.1"})
    assert response.status_code == 502

    def raise_transient():
        raise AuthTransientError("refresh failed")

    monkeypatch.setattr(
        analytics_identity,
        "get_token_manager",
        lambda backend_url: SimpleNamespace(get_access_token=raise_transient),
    )
    response = _call("/api/openbase/analytics/identify", {"ga_client_id": "GA1.1.1"})
    assert response.status_code == 502


def test_identify_tolerates_non_json_cloud_body(monkeypatch) -> None:
    _stub_cloud(monkeypatch)
    monkeypatch.setattr(
        analytics_identity.httpx,
        "post",
        lambda *a, **k: httpx.Response(200, text="ok"),
    )

    response = _call("/api/openbase/analytics/identify", {"ga_client_id": "GA1.1.1"})

    assert response.status_code == 200
    assert response.data == {"analytics_key": None, "message": ""}
