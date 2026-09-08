"""Tests for the single-owner identity pin (security finding C2).

The local server must only accept tokens for the account that ran
``openbase-coder login``; any other valid cloud JWT must be rejected.
"""

import base64
import json
from unittest import mock

import pytest
from rest_framework import exceptions

from openbase_coder_cli.config import authentication as auth_module
from openbase_coder_cli.config import token_manager as tm_module
from openbase_coder_cli.config.token_manager import TokenManager


def make_jwt(claims: dict) -> str:
    """Build a structurally-valid JWT (signature is irrelevant here)."""

    def seg(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'alg': 'RS256', 'kid': 'k'})}.{seg(claims)}.sig"


@pytest.fixture
def auth_path(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    monkeypatch.setattr(tm_module, "AUTH_JSON_PATH", path)
    return path


def login_as(auth_path, sub="user-1", email="owner@example.com"):
    TokenManager("https://backend.example.com").store_tokens(
        access_token=make_jwt({"sub": sub, "email": email}),
        refresh_token=make_jwt({"sub": sub}),
    )


# ---- get_owner_identity ----------------------------------------------------


def test_owner_identity_decoded_from_stored_token(auth_path):
    login_as(auth_path, sub="abc123", email="Owner@Example.com")
    identity = TokenManager("https://backend.example.com").get_owner_identity()
    assert identity == {"sub": "abc123", "email": "owner@example.com"}


def test_owner_identity_empty_when_logged_out(auth_path):
    assert TokenManager("https://backend.example.com").get_owner_identity() == {}


# ---- is_owner_identity / enforce_owner_identity ----------------------------


def _patch_owner(monkeypatch, owner):
    monkeypatch.setattr(
        auth_module,
        "get_token_manager",
        lambda *a, **k: mock.Mock(get_owner_identity=lambda: owner),
        raising=False,
    )
    # is_owner_identity imports get_token_manager lazily from token_manager.
    monkeypatch.setattr(
        tm_module,
        "get_token_manager",
        lambda *a, **k: mock.Mock(get_owner_identity=lambda: owner),
    )


def test_matching_subject_is_authorized(monkeypatch):
    _patch_owner(monkeypatch, {"sub": "user-1", "email": "owner@example.com"})
    assert auth_module.is_owner_identity({"sub": "user-1"}) is True
    auth_module.enforce_owner_identity({"sub": "user-1"})  # no raise


def test_matching_email_is_authorized(monkeypatch):
    _patch_owner(monkeypatch, {"sub": "user-1", "email": "owner@example.com"})
    # Different sub format (e.g. auth/session fallback) but same email.
    assert (
        auth_module.is_owner_identity({"sub": "9", "email": "Owner@Example.com"})
        is True
    )


def test_other_subject_is_rejected(monkeypatch):
    _patch_owner(monkeypatch, {"sub": "user-1", "email": "owner@example.com"})
    assert auth_module.is_owner_identity({"sub": "attacker-2"}) is False
    with pytest.raises(exceptions.AuthenticationFailed):
        auth_module.enforce_owner_identity(
            {"sub": "attacker-2", "email": "evil@example.com"}
        )


def test_no_owner_rejects_all(monkeypatch):
    _patch_owner(monkeypatch, {})
    assert auth_module.is_owner_identity({"sub": "user-1"}) is False
    with pytest.raises(exceptions.AuthenticationFailed):
        auth_module.enforce_owner_identity({"sub": "user-1"})


def test_environment_cannot_bypass_owner_identity(monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_ALLOW_ANY_SUBJECT", "true")
    _patch_owner(monkeypatch, {"sub": "user-1", "email": "owner@example.com"})

    foreign_claims = {"sub": "user-2", "email": "other@example.com"}
    assert auth_module.is_owner_identity(foreign_claims) is False
    with pytest.raises(exceptions.AuthenticationFailed):
        auth_module.enforce_owner_identity(foreign_claims)


# ---- end-to-end through JWTAuthentication.authenticate ---------------------


def test_authenticate_rejects_foreign_token_despite_environment(auth_path, monkeypatch):
    login_as(auth_path, sub="owner-sub", email="owner@example.com")
    monkeypatch.setenv("OPENBASE_CODER_CLI_ALLOW_ANY_SUBJECT", "true")

    foreign = make_jwt({"sub": "attacker-sub", "email": "attacker@example.com"})
    monkeypatch.setattr(
        auth_module,
        "_get_validator",
        lambda: mock.Mock(
            validate=lambda _t: {"sub": "attacker-sub", "email": "attacker@example.com"}
        ),
    )

    factory_request = mock.Mock()
    factory_request.META = {"HTTP_AUTHORIZATION": f"Bearer {foreign}"}
    with pytest.raises(exceptions.AuthenticationFailed):
        auth_module.JWTAuthentication().authenticate(factory_request)


def test_authenticate_accepts_owner_token(auth_path, monkeypatch):
    login_as(auth_path, sub="owner-sub", email="owner@example.com")

    owner_token = make_jwt({"sub": "owner-sub", "email": "owner@example.com"})
    monkeypatch.setattr(
        auth_module,
        "_get_validator",
        lambda: mock.Mock(
            validate=lambda _t: {"sub": "owner-sub", "email": "owner@example.com"}
        ),
    )
    monkeypatch.setattr(
        auth_module, "_get_or_create_user", lambda *, sub: mock.Mock(sub=sub)
    )

    factory_request = mock.Mock()
    factory_request.META = {"HTTP_AUTHORIZATION": f"Bearer {owner_token}"}
    user, claims = auth_module.JWTAuthentication().authenticate(factory_request)
    assert claims["sub"] == "owner-sub"
