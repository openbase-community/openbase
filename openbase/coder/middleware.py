from __future__ import annotations

from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser

from openbase.config import settings

User = get_user_model()


class TokenAuthMiddleware:
    """
    Custom authentication middleware for WebSocket connections.

    Supports:
    - OpenbaseTokenAuthentication (API token from settings)
    - DRF Token Authentication (if rest_framework.authtoken is installed)
    - Session Authentication (from Django sessions)

    Authentication can be provided via:
    - Query string: ?token=YOUR_TOKEN
    - Authorization header: Authorization: Bearer YOUR_TOKEN
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Only process WebSocket connections
        if scope["type"] != "websocket":
            return await self.app(scope, receive, send)

        # Check if user is already authenticated via session
        if "user" in scope and not isinstance(scope["user"], AnonymousUser):
            # User is already authenticated via session
            return await self.app(scope, receive, send)

        # Extract token from query string or headers
        token = await self._get_token_from_scope(scope)

        # Try to authenticate with token
        if token:
            user = await self._authenticate_with_token(token)
            if user:
                scope["user"] = user
                return await self.app(scope, receive, send)

        # Set anonymous user if no authentication found
        if "user" not in scope:
            scope["user"] = AnonymousUser()

        return await self.app(scope, receive, send)

    async def _get_token_from_scope(self, scope):
        """Extract token from query string or headers"""
        # Check query string for token
        query_string = scope.get("query_string", b"").decode("utf-8")
        if query_string:
            params = parse_qs(query_string)
            token_list = params.get("token", [])
            if token_list:
                return token_list[0]

        # Check headers for Authorization header
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("utf-8")
        if auth_header:
            try:
                token_type, token = auth_header.split()
                if token_type.lower() == "bearer":
                    return token
            except ValueError:
                pass

        return None

    async def _authenticate_with_token(self, auth_token):
        """Authenticate using the provided token"""
        # Try OpenbaseTokenAuthentication first
        if auth_token == settings.OPENBASE_API_TOKEN:
            # Get the first user for API token auth
            user = await database_sync_to_async(User.objects.first)()
            if user:
                return user

        # Try DRF token auth if available
        try:
            from rest_framework.authtoken.models import Token  # noqa: PLC0415

            token_obj = await database_sync_to_async(
                Token.objects.select_related("user").filter(key=auth_token).first
            )()

            if token_obj:
                return token_obj.user
        except ImportError:
            # rest_framework.authtoken not installed, skip
            pass

        return None


def token_auth_middleware_stack(inner):
    """
    Convenience method to wrap an ASGI application with the token auth middleware.

    Usage:
        token_auth_middleware_stack(URLRouter(...))
    """
    return TokenAuthMiddleware(inner)