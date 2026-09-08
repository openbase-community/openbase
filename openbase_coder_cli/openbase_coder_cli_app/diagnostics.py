"""Health, diagnostics, and device discovery API views."""

from __future__ import annotations

import json
import logging
import re
import time

from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from openbase_coder_cli.openbase_coder_cli_app.common import _auth_debug_value
from openbase_coder_cli.paths import DEFAULT_LOG_DIR
from openbase_coder_cli.services.tailnet_devices import tailnet_devices_payload

logger = logging.getLogger(__name__)

IOS_LOG_UPLOAD_FILENAME = "ios-app.log"
IOS_LOG_UPLOAD_MAX_ENTRIES = 1000
IOS_LOG_UPLOAD_MAX_BYTES = 2 * 1024 * 1024
REDACTED_VALUE = "<redacted>"
REDACTED_EMAIL_VALUE = "<redacted-email>"
SENSITIVE_KEY_SUFFIXES = ("password", "secret", "key", "token")
SENSITIVE_KEY_NAMES = {"authorization", "name", "x-session-token"}
SENSITIVE_KEY_PATTERN = r"(?:password|secret|key|token)"


class IOSLogEntrySerializer(serializers.Serializer):
    timestamp = serializers.CharField(required=False, allow_blank=True, max_length=64)
    component = serializers.CharField(required=False, allow_blank=True, max_length=128)
    message = serializers.CharField(required=False, allow_blank=True)
    metadata = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        required=False,
    )
    line = serializers.CharField(required=False, allow_blank=True)


class IOSLogUploadSerializer(serializers.Serializer):
    entries = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=False,
        max_length=IOS_LOG_UPLOAD_MAX_ENTRIES,
    )
    device = serializers.DictField(required=False)

    def validate_entries(self, entries):
        validated_entries = []
        for entry in entries:
            serializer = IOSLogEntrySerializer(data=entry)
            serializer.is_valid(raise_exception=True)
            validated_entries.append(serializer.validated_data)
        return validated_entries

    def validate(self, attrs):
        encoded_size = len(
            json.dumps(attrs, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        if encoded_size > IOS_LOG_UPLOAD_MAX_BYTES:
            raise serializers.ValidationError(
                {
                    "entries": (
                        "The log upload exceeds the 2 MiB batch limit; "
                        "upload fewer recent entries."
                    )
                }
            )
        return attrs


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    """Health check endpoint."""
    logger.info(
        "health_check request path=%s auth=%s",
        request.path,
        _auth_debug_value(request),
    )
    return Response({"status": "ok"}, status=status.HTTP_200_OK)


@api_view(["POST"])
def ios_logs_upload(request):
    """Append uploaded iOS diagnostics to the Openbase Coder log directory."""
    serializer = IOSLogUploadSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    entries = [_sanitize_mapping(entry) for entry in serializer.validated_data["entries"]]
    device = _sanitize_mapping(serializer.validated_data.get("device") or {})
    uploaded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DEFAULT_LOG_DIR / IOS_LOG_UPLOAD_FILENAME
    with log_path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(
                json.dumps(
                    {
                        "uploaded_at": uploaded_at,
                        "source": "ios",
                        "device": device,
                        "entry": entry,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )

    logger.info("ios_logs_upload wrote count=%s path=%s", len(entries), log_path)
    return Response(
        {
            "uploaded_count": len(entries),
            "log_path": str(log_path),
        },
        status=status.HTTP_201_CREATED,
    )


def _is_sensitive_key(name: str) -> bool:
    lowered = name.lower()
    return lowered in SENSITIVE_KEY_NAMES or lowered.endswith(SENSITIVE_KEY_SUFFIXES)


def _redact_sensitive_text(value: str) -> str:
    redacted = re.sub(
        rf'("[\w-]*{SENSITIVE_KEY_PATTERN}"\s*:\s*)"[^"]*"',
        rf"\1\"{REDACTED_VALUE}\"",
        value,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(
        rf"([\w-]*{SENSITIVE_KEY_PATTERN}=)[^&\s\"]+",
        rf"\1{REDACTED_VALUE}",
        redacted,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(
        r"(Bearer\s+)[A-Za-z0-9._~+/\-=]+",
        rf"\1{REDACTED_VALUE}",
        redacted,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        REDACTED_EMAIL_VALUE,
        redacted,
        flags=re.IGNORECASE,
    )


def _sanitize_value(key: str, value):
    if isinstance(value, dict):
        return _sanitize_mapping(value)
    if isinstance(value, list):
        return [_sanitize_value(key, item) for item in value]
    if _is_sensitive_key(key):
        return REDACTED_VALUE
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


def _sanitize_mapping(payload: dict) -> dict:
    return {key: _sanitize_value(str(key), value) for key, value in payload.items()}


@api_view(["GET"])
def devices_list(request):
    """Discover tailnet devices and identify Openbase Coder hosts."""
    return Response(tailnet_devices_payload(), status=status.HTTP_200_OK)
