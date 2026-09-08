"""WebSocket authentication middleware."""

from __future__ import annotations

import logging
import secrets
from urllib.parse import parse_qs

import httpx
from channels.middleware import BaseMiddleware
from django.conf import settings

from openbase_coder_cli.config.authentication import is_owner_identity
from openbase_coder_cli.config.jwt_validation import InvalidTokenError, JWKSValidator
from openbase_coder_cli.config.local_api_token import get_local_api_token
from openbase_coder_cli.config.token_manager import decode_jwt_claims_unverified

logger = logging.getLogger(__name__)

_ws_validator: JWKSValidator | None = None


def _get_ws_validator() -> JWKSValidator | None:
    global _ws_validator
    if _ws_validator is not None:
        return _ws_validator

    web_backend_url = getattr(settings, "WEB_BACKEND_URL", "")
    if not web_backend_url:
        return None

    _ws_validator = JWKSValidator(
        getattr(settings, "JWT_JWKS_URL", f"{web_backend_url}/.well-known/jwks.json"),
        expected_issuer=getattr(settings, "JWT_ISSUER", web_backend_url),
        expected_audience=getattr(settings, "JWT_AUDIENCE", "openbase-coder-cli"),
    )
    return _ws_validator


def _validate_ws_via_auth_session(token: str) -> bool:
    web_backend_url = getattr(settings, "WEB_BACKEND_URL", "").rstrip("/")
    if not web_backend_url:
        return False

    session_url = getattr(
        settings,
        "JWT_AUTH_SESSION_URL",
        f"{web_backend_url}/_allauth/app/v1/auth/session",
    )
    try:
        resp = httpx.get(
            session_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=10,
        )
    except httpx.HTTPError:
        return False

    if resp.status_code != 200:
        return False

    try:
        payload = resp.json()
    except ValueError:
        return False

    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    return bool(meta.get("is_authenticated"))


async def _validate_ws_via_auth_session_async(token: str) -> bool:
    web_backend_url = getattr(settings, "WEB_BACKEND_URL", "").rstrip("/")
    if not web_backend_url:
        return False

    session_url = getattr(
        settings,
        "JWT_AUTH_SESSION_URL",
        f"{web_backend_url}/_allauth/app/v1/auth/session",
    )
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                session_url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                timeout=10,
            )
    except httpx.HTTPError:
        return False

    if resp.status_code != 200:
        return False

    try:
        payload = resp.json()
    except ValueError:
        return False

    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    return bool(meta.get("is_authenticated"))


class TokenAuthMiddleware(BaseMiddleware):
    """Authenticate WebSocket connections via query-string token.

    Accepts a valid cloud JWT.
    JWTs are validated locally via JWKS, then via auth/session fallback.
    """

    async def __call__(self, scope, receive, send):
        query_string = scope.get("query_string", b"").decode("utf-8")
        params = parse_qs(query_string)
        token = params.get("token", [None])[0]

        scope["user"] = None

        if token and token.count(".") != 2:
            if secrets.compare_digest(token, get_local_api_token()):
                scope["user"] = "authenticated"
        elif token:
            validator = _get_ws_validator()
            if validator:
                claims: dict | None = None
                try:
                    claims = await validator.validate_async(token)
                except InvalidTokenError:
                    if await _validate_ws_via_auth_session_async(token):
                        # Backend vouched for the signature; read identity
                        # claims from the (now-trusted) token to pin owner.
                        claims = decode_jwt_claims_unverified(token)
                    else:
                        logger.debug("WebSocket JWT validation failed")

                if claims is not None:
                    if is_owner_identity(claims):
                        scope["user"] = "authenticated"
                    else:
                        logger.warning(
                            "Rejecting WebSocket: token identity is not the "
                            "server owner"
                        )

        return await super().__call__(scope, receive, send)
