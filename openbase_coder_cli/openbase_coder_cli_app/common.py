"""Shared helpers for CLI API views."""

from __future__ import annotations

import functools
from typing import Any

from asgiref.sync import sync_to_async
from rest_framework import serializers


def offloaded_view(view):
    """Run a sync DRF view off Django's shared thread-sensitive executor.

    Under ASGI, Django runs synchronous views through
    ``sync_to_async(thread_sensitive=True)``, which funnels every in-flight
    request through a single shared executor thread and serializes them —
    so a page that fires several API calls at mount has them stack up. This
    decorator makes the view an ``async`` callable (which Django awaits
    directly on the event loop) and offloads the real work with
    ``thread_sensitive=False`` so concurrent requests run on separate pool
    threads.

    Nested ``async_to_sync`` calls inside the view (e.g. the out-of-process
    session manager) still route their coroutines back to the main event
    loop via asgiref's ``main_event_loop`` threadlocal, so the app-server
    connection keeps its loop affinity and correctness is preserved.

    Apply it outside ``@api_view`` so it wraps the finished DRF view::

        @offloaded_view
        @api_view(["GET"])
        def my_view(request):
            ...
    """
    async_view = sync_to_async(view, thread_sensitive=False)

    @functools.wraps(view)
    async def wrapper(request, *args, **kwargs):
        return await async_view(request, *args, **kwargs)

    # DRF's ``as_view()`` marks the view CSRF-exempt; preserve that on the
    # async wrapper Django actually routes to.
    wrapper.csrf_exempt = getattr(view, "csrf_exempt", True)
    return wrapper


class ExactFieldsSerializer(serializers.Serializer):
    """Reject unexpected input fields instead of silently ignoring them."""

    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {field: ["Unknown field."] for field in sorted(unknown)}
            )
        return super().to_internal_value(data)


def _request_identity(request) -> str:
    if isinstance(request.auth, dict):
        email = str(request.auth.get("email", "")).strip()
        if email:
            return email

    email = str(getattr(request.user, "email", "") or "").strip()
    if email:
        return email

    username = str(getattr(request.user, "username", "") or "").strip()
    if username:
        return username

    return f"user-{request.user.pk}"


def _auth_debug_value(request) -> str:
    if isinstance(request.auth, dict):
        return "jwt"
    if request.auth:
        return type(request.auth).__name__
    return "none"


def _clean_serializer_data(data: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str) and not value.strip():
            continue
        cleaned[key] = value
    return cleaned
