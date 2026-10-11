"""A validation outage must not look like expired device credentials."""

import os
from unittest.mock import Mock

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402

django.setup()

import httpx  # noqa: E402
import pytest  # noqa: E402
from rest_framework.response import Response  # noqa: E402
from rest_framework.test import APIRequestFactory  # noqa: E402
from rest_framework.views import APIView  # noqa: E402

from openbase_coder_cli.config import authentication as auth  # noqa: E402


class ProtectedView(APIView):
    authentication_classes = [auth.JWTAuthentication]
    permission_classes = []

    def get(self, request):
        return Response({"subject": request.auth["sub"]})


@pytest.fixture
def fallback(monkeypatch):
    monkeypatch.setattr(auth.settings, "WEB_BACKEND_URL", "https://auth.example.test")
    validator = Mock()
    validator.validate.side_effect = auth.InvalidTokenError("Invalid issuer")
    monkeypatch.setattr(auth, "_get_validator", lambda: validator)
    monkeypatch.setattr(auth, "get_token_manager_owner", lambda: {"sub": "owner"})
    monkeypatch.setattr(
        auth, "is_owner_identity", lambda claims: claims["sub"] == "owner"
    )
    create_user = Mock(return_value=Mock(is_authenticated=True))
    monkeypatch.setattr(auth, "_get_or_create_user", create_user)
    upstream = Mock()
    monkeypatch.setattr(auth.httpx, "get", upstream)
    return upstream, create_user


def request():
    return ProtectedView.as_view()(
        APIRequestFactory().get(
            "/protected", HTTP_AUTHORIZATION="Bearer test.jwt.token"
        )
    )


def accepted(subject="owner"):
    return httpx.Response(
        200,
        json={"meta": {"is_authenticated": True}, "data": {"user": {"id": subject}}},
    )


@pytest.mark.parametrize("status", [400, 404, 429, 500, 502, 503])
def test_upstream_failure_is_retryable_not_unauthorized(fallback, status, caplog):
    upstream, create_user = fallback
    upstream.return_value = httpx.Response(status, text="private response body")
    response = request()
    assert response.status_code == 503
    assert response.data["detail"].code == "authentication_unavailable"
    assert "WWW-Authenticate" not in response
    assert f"http_{status}" in caplog.text
    assert "private response body" not in caplog.text
    create_user.assert_not_called()


def test_transport_failure_recovers_with_same_credentials(fallback, caplog):
    upstream, create_user = fallback
    upstream.side_effect = [
        httpx.ReadTimeout("private token test.jwt.token"),
        accepted(),
    ]
    assert request().status_code == 503
    create_user.assert_not_called()
    response = request()
    assert response.status_code == 200
    assert response.data == {"subject": "owner"}
    assert upstream.call_args_list[0] == upstream.call_args_list[1]
    assert "transport_error" in caplog.text
    assert "test.jwt.token" not in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"meta": None},
        {"meta": {"is_authenticated": "true"}},
        {"meta": {"is_authenticated": True}, "data": None},
        {"meta": {"is_authenticated": True}, "data": {"user": None}},
        {"meta": {"is_authenticated": True}, "data": {"user": {}}},
    ],
)
def test_malformed_success_fails_closed_as_unavailable(fallback, payload):
    upstream, create_user = fallback
    upstream.return_value = httpx.Response(200, json=payload)
    assert request().status_code == 503
    create_user.assert_not_called()


def test_non_json_fails_closed_as_unavailable(fallback):
    upstream, create_user = fallback
    upstream.return_value = httpx.Response(200, text="<html>unavailable</html>")
    assert request().status_code == 503
    create_user.assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
def test_real_token_rejection_stays_unauthorized(fallback, status):
    upstream, create_user = fallback
    upstream.return_value = httpx.Response(status)
    response = request()
    assert response.status_code == 401
    assert response["WWW-Authenticate"] == "Bearer"
    assert str(status) in str(response.data["detail"])
    assert "Invalid issuer" not in str(response.data)
    create_user.assert_not_called()


def test_explicit_unauthenticated_session_stays_unauthorized(fallback):
    upstream, create_user = fallback
    upstream.return_value = httpx.Response(
        200, json={"meta": {"is_authenticated": False}}
    )
    assert request().status_code == 401
    create_user.assert_not_called()


def test_valid_foreign_session_still_rejected(fallback):
    upstream, create_user = fallback
    upstream.return_value = accepted("other-user")
    response = request()
    assert response.status_code == 401
    assert "not authorized" in str(response.data["detail"])
    create_user.assert_not_called()
