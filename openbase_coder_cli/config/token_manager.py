"""
Manage locally-stored JWT tokens for CLI-to-service authentication.

Handles loading/saving refresh tokens to ~/.openbase/auth.json and
refreshing access tokens from the web backend's allauth API.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Generator
from typing import Any

import httpx

from openbase_coder_cli.file_lock import LOCK_EX, LOCK_UN, flock
from openbase_coder_cli.paths import AUTH_JSON_PATH

logger = logging.getLogger(__name__)


class AuthLoginRequiredError(RuntimeError):
    """The stored refresh token is missing or was rejected by the backend."""


class AuthTransientError(RuntimeError):
    """Token refresh failed for a retryable reason (network, backend 5xx)."""


def decode_jwt_claims_unverified(token: str) -> dict[str, Any]:
    """Decode a JWT payload WITHOUT verifying its signature.

    Only valid for reading identity claims from tokens we already trust
    (our own on-disk credentials). Never use this to authorize an inbound
    token — inbound tokens must go through full JWKS signature validation.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    segment = parts[1]
    padded = segment + "=" * (-len(segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded)
        claims = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


# Refresh the access token 60 seconds before it expires
_REFRESH_MARGIN_SECONDS = 60
# How long a validated login_status() result may be reused before re-checking
# with the backend.
_LOGIN_STATUS_CACHE_SECONDS = 60
LOGIN_STATUS_LOGGED_IN = "logged_in"
LOGIN_STATUS_LOGGED_OUT = "logged_out"
LOGIN_STATUS_LOGIN_EXPIRED = "login_expired"
DEFAULT_OAUTH_CLIENT_ID = "openbase-coder-cli"
DEFAULT_OAUTH_REDIRECT_URI = "http://127.0.0.1:52807/oauth/callback"
DEFAULT_WEB_BACKEND_URL = "https://app.openbase.cloud"


class TokenManager:
    """Manages JWT access + refresh tokens stored on disk.

    Usage::

        mgr = TokenManager(web_backend_url="https://backend.example.com")
        mgr.load()
        token = mgr.get_access_token()  # auto-refreshes if needed
    """

    def __init__(self, web_backend_url: str):
        self._web_backend_url = web_backend_url.rstrip("/")
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._access_expires_at: float = 0  # epoch seconds
        self._refresh_rejected_at: float = 0  # epoch seconds; 0 = not rejected
        self._login_status_cache: dict[str, Any] | None = None
        # Serializes refresh across threads in this process; the flock in
        # _file_lock() serializes across processes sharing auth.json.
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def _file_lock(self) -> Generator[None, None, None]:
        """Hold an exclusive cross-process lock guarding auth.json."""
        lock_path = AUTH_JSON_PATH.with_suffix(".json.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            flock(fd, LOCK_EX)
            yield
        finally:
            flock(fd, LOCK_UN)
            os.close(fd)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load tokens from disk (if present)."""
        if not AUTH_JSON_PATH.is_file():
            self._access_token = ""
            self._refresh_token = ""
            self._access_expires_at = 0
            self._refresh_rejected_at = 0
            return
        try:
            data = json.loads(AUTH_JSON_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            # A torn read while another process writes; keep the in-memory
            # tokens rather than treating the user as logged out.
            logger.warning(
                "Could not parse %s; keeping in-memory tokens", AUTH_JSON_PATH
            )
            return
        self._access_token = data.get("access_token", "")
        self._refresh_token = data.get("refresh_token", "")
        self._access_expires_at = data.get("access_expires_at", 0)
        self._refresh_rejected_at = data.get("refresh_rejected_at", 0)

    def save(self) -> None:
        """Persist current tokens to disk atomically."""
        AUTH_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "access_expires_at": self._access_expires_at,
                "refresh_rejected_at": self._refresh_rejected_at,
            },
            indent=2,
        )
        tmp_path = AUTH_JSON_PATH.with_suffix(f".json.tmp{os.getpid()}")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
            os.replace(tmp_path, AUTH_JSON_PATH)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_path)

    def clear(self) -> None:
        """Remove stored tokens."""
        with self._lock, self._file_lock():
            self._access_token = ""
            self._refresh_token = ""
            self._access_expires_at = 0
            self._refresh_rejected_at = 0
            self._login_status_cache = None
            if AUTH_JSON_PATH.is_file():
                AUTH_JSON_PATH.unlink()

    # ------------------------------------------------------------------
    # Token state
    # ------------------------------------------------------------------

    def store_tokens(
        self,
        *,
        access_token: str,
        refresh_token: str,
        expires_in: int = 300,
    ) -> None:
        """Store tokens received from authentication and persist to disk."""
        with self._lock, self._file_lock():
            self._access_token = access_token
            self._refresh_token = refresh_token
            self._access_expires_at = time.time() + expires_in
            self._refresh_rejected_at = 0
            self._login_status_cache = None
            self.save()

    @property
    def is_logged_in(self) -> bool:
        with self._lock:
            self.load()
            return bool(self._refresh_token)

    def _access_is_valid(self) -> bool:
        return bool(self._access_token) and time.time() < (
            self._access_expires_at - _REFRESH_MARGIN_SECONDS
        )

    # ------------------------------------------------------------------
    # Access token (with auto-refresh)
    # ------------------------------------------------------------------

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing from the backend if needed.

        Raises ``AuthLoginRequiredError`` if no refresh token is available or
        the backend rejected it, and ``AuthTransientError`` for retryable
        failures (network errors, backend 5xx).
        """
        with self._lock:
            self.load()
            if self._access_is_valid():
                return self._access_token

            with self._file_lock():
                # Another process may have refreshed while we waited.
                self.load()
                if self._access_is_valid():
                    return self._access_token

                if not self._refresh_token:
                    raise AuthLoginRequiredError(
                        "No refresh token available. Run 'openbase-coder login' first."
                    )

                self._do_refresh()
                return self._access_token

    def _do_refresh(self) -> None:
        """Refresh the access token using the stored refresh token.

        Must be called with the thread and file locks held.
        """
        url = f"{self._web_backend_url}/_allauth/app/v1/tokens/refresh"
        try:
            resp = httpx.post(
                url,
                json={"refresh_token": self._refresh_token},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            raise AuthTransientError(f"Token refresh failed: {exc}") from exc

        if resp.status_code in (400, 401, 403):
            # The refresh token was rotated away or expired; only a new
            # login can recover. Persist the rejection so every process
            # (and mere presence checks on auth.json) can see the login is
            # dead without repeating this network call.
            self._refresh_rejected_at = time.time()
            self.save()
            raise AuthLoginRequiredError(
                "Refresh token was rejected. Run 'openbase-coder login' again."
            )
        if resp.status_code >= 500:
            raise AuthTransientError(
                f"Token refresh failed with backend status {resp.status_code}"
            )
        resp.raise_for_status()

        data = resp.json()
        meta = data.get("meta", {}) if isinstance(data, dict) else {}
        payload = data.get("data", {}) if isinstance(data, dict) else {}
        self._access_token = (
            meta.get("access_token")
            or payload.get("access_token")
            or data.get("access_token", "")
        )
        # Some allauth versions return a new refresh token
        new_refresh = (
            meta.get("refresh_token")
            or payload.get("refresh_token")
            or data.get("refresh_token")
        )
        if new_refresh:
            self._refresh_token = new_refresh
        expires_in = (
            meta.get("access_token_expires_in")
            or payload.get("access_token_expires_in")
            or 300
        )
        self._access_expires_at = time.time() + expires_in
        self._refresh_rejected_at = 0
        self.save()
        logger.info("Refreshed JWT access token")

    @property
    def has_refresh_token(self) -> bool:
        """Whether a refresh token is merely PRESENT on disk.

        Presence is not login state: the token may already be rejected by
        the cloud. Surfaces that report "logged in with Openbase Cloud"
        must use :meth:`login_status` instead.
        """
        with self._lock:
            self.load()
            return bool(self._refresh_token)

    def login_status(
        self, *, max_age_seconds: float = _LOGIN_STATUS_CACHE_SECONDS
    ) -> dict[str, Any]:
        """Validated Openbase Cloud login status — the single source of truth.

        Returns ``{"status", "validated", "detail"}`` where ``status`` is one
        of ``logged_in`` / ``logged_out`` / ``login_expired``. Unlike
        ``has_refresh_token`` (mere token presence), this reflects whether the
        cloud still accepts the stored credentials: a definitive refresh
        rejection is remembered on disk (``refresh_rejected_at``), so every
        process answers consistently without repeating the network call.
        ``validated`` is False only when the backend was unreachable and token
        presence is the best available answer.
        """
        with self._lock:
            self.load()
            if not self._refresh_token:
                return {
                    "status": LOGIN_STATUS_LOGGED_OUT,
                    "validated": True,
                    "detail": "Not logged in. Run 'openbase-coder login'.",
                }
            if self._refresh_rejected_at:
                return {
                    "status": LOGIN_STATUS_LOGIN_EXPIRED,
                    "validated": True,
                    "detail": (
                        "Openbase Cloud rejected the stored login. "
                        "Run 'openbase-coder login' again."
                    ),
                }
            if self._access_is_valid():
                return {
                    "status": LOGIN_STATUS_LOGGED_IN,
                    "validated": True,
                    "detail": "",
                }

            now = time.time()
            cached = self._login_status_cache
            if (
                cached
                and cached["refresh_token"] == self._refresh_token
                and now - cached["checked_at"] < max_age_seconds
            ):
                return dict(cached["result"])

            refresh_token = self._refresh_token
            try:
                self.get_access_token()
            except AuthLoginRequiredError:
                # _do_refresh persisted refresh_rejected_at for us.
                result = {
                    "status": LOGIN_STATUS_LOGIN_EXPIRED,
                    "validated": True,
                    "detail": (
                        "Openbase Cloud rejected the stored login. "
                        "Run 'openbase-coder login' again."
                    ),
                }
            except AuthTransientError as exc:
                result = {
                    "status": LOGIN_STATUS_LOGGED_IN,
                    "validated": False,
                    "detail": (
                        f"Could not reach Openbase Cloud to validate the login: {exc}"
                    ),
                }
            else:
                result = {
                    "status": LOGIN_STATUS_LOGGED_IN,
                    "validated": True,
                    "detail": "",
                }
            self._login_status_cache = {
                "checked_at": now,
                "refresh_token": refresh_token,
                "result": dict(result),
            }
            return result

    def get_owner_identity(self) -> dict[str, str]:
        """Return the ``{sub, email}`` of the account that owns this server.

        Derived from the credentials written by ``openbase-coder login``.
        This is the single authorized identity for the local server: only
        tokens for the same subject may use authenticated endpoints. Returns
        an empty dict when no one is logged in (server has no owner).
        """
        with self._lock:
            self.load()
            for token in (self._access_token, self._refresh_token):
                if not token:
                    continue
                claims = decode_jwt_claims_unverified(token)
                sub = claims.get("sub")
                if sub:
                    identity = {"sub": str(sub)}
                    if claims.get("email"):
                        identity["email"] = str(claims["email"]).strip().lower()
                    return identity
            return {}

    def get_access_token_payload(self) -> dict[str, Any]:
        with self._lock:
            token = self.get_access_token()
            expires_in = max(0, int(self._access_expires_at - time.time()))
            return {
                "access_token": token,
                "access_token_expires_in": expires_in,
            }


class CloudAccessTokenAuth(httpx.Auth):
    """HTTPX auth helper that injects a fresh cloud JWT per request."""

    requires_request_body = True

    def __init__(self, manager: TokenManager):
        self._manager = manager

    def _apply(self, request: httpx.Request) -> None:
        token = self._manager.get_access_token()
        request.headers["Authorization"] = f"Bearer {token}"

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        self._apply(request)
        yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        self._apply(request)
        yield request


def get_cloud_auth_headers(web_backend_url: str | None = None) -> dict[str, str]:
    token = get_token_manager(web_backend_url).get_access_token()
    return {"Authorization": f"Bearer {token}"}


def create_pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def create_pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


# ------------------------------------------------------------------
# Singleton
# ------------------------------------------------------------------

_instance: TokenManager | None = None


def get_token_manager(web_backend_url: str | None = None) -> TokenManager:
    """Return the global TokenManager, creating it if necessary.

    ``web_backend_url`` is required on first call (or taken from Django
    settings when imported in the server context).
    """
    global _instance
    if _instance is not None:
        return _instance

    if web_backend_url is None:
        web_backend_url = os.environ.get(
            "OPENBASE_CODER_CLI_WEB_BACKEND_URL",
            DEFAULT_WEB_BACKEND_URL,
        ).strip()

    if web_backend_url is None or not web_backend_url:
        try:
            from django.conf import settings
        except Exception:
            settings = None
        if settings is not None:
            web_backend_url = getattr(settings, "WEB_BACKEND_URL", "")

    if not web_backend_url:
        raise RuntimeError(
            "WEB_BACKEND_URL is not configured. "
            "Set OPENBASE_CODER_CLI_WEB_BACKEND_URL in your environment."
        )

    _instance = TokenManager(web_backend_url)
    _instance.load()
    return _instance
