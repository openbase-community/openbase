"""Shared helpers for CLI API views."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers


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
