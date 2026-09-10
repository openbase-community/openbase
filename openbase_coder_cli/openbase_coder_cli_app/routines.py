"""Routine (loop) management API views, including trigger and event ingest."""

from __future__ import annotations

from asgiref.sync import async_to_sync
from rest_framework import serializers, status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from openbase_coder_cli.openbase_coder_cli_app.common import _clean_serializer_data
from openbase_coder_cli.thread_sync.session_manager import get_session_manager

# Never hand inbound credentials to the delivery layer; the capability token
# in the URL plus the optional per-trigger HMAC are the only trust inputs.
_INGEST_HEADER_DENYLIST = {"authorization", "cookie", "proxy-authorization"}


class RoutineSerializer(serializers.Serializer):
    name = serializers.CharField(trim_whitespace=True, max_length=256)
    kind = serializers.ChoiceField(
        choices=["agent", "command"],
        required=False,
    )
    prompt = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=False,
    )
    command = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=False,
    )
    commandTimeoutSeconds = serializers.IntegerField(
        required=False,
        min_value=1,
    )
    time = serializers.CharField(
        required=False,
        allow_blank=False,
        trim_whitespace=True,
        max_length=5,
    )
    scheduleType = serializers.ChoiceField(
        choices=["daily", "interval"],
        required=False,
    )
    intervalSeconds = serializers.IntegerField(
        required=False,
        min_value=5,
    )
    timezone = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=128,
    )
    enabled = serializers.BooleanField(required=False)
    targetName = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=256,
    )
    threadId = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=256,
    )
    freshThreadPerRun = serializers.BooleanField(required=False)
    cwd = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=4096,
    )
    approvalPolicy = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=64,
    )
    sandboxType = serializers.ChoiceField(
        choices=["readOnly", "workspaceWrite", "dangerFullAccess"],
        required=False,
    )
    mode = serializers.ChoiceField(choices=["default", "plan"], required=False)
    model = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=128,
    )
    reasoningEffort = serializers.ChoiceField(
        choices=["low", "medium", "high", "xhigh"],
        required=False,
    )
    serviceTier = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=128,
    )
    developerInstructions = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        trim_whitespace=False,
    )

    def validate_time(self, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2:
            raise serializers.ValidationError("Use HH:MM in 24-hour time.")
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError:
            raise serializers.ValidationError("Use HH:MM in 24-hour time.") from None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise serializers.ValidationError("Use HH:MM in 24-hour time.")
        return f"{hour:02d}:{minute:02d}"

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if (
            attrs.get("intervalSeconds") is not None
            and attrs.get("scheduleType") is None
        ):
            attrs["scheduleType"] = "interval"
        return attrs


class RoutineCreateSerializer(RoutineSerializer):
    prompt = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=False
    )
    time = serializers.CharField(
        required=False,
        allow_blank=False,
        trim_whitespace=True,
        max_length=5,
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        kind = attrs.get("kind") or "agent"
        if kind == "agent" and not attrs.get("prompt"):
            raise serializers.ValidationError(
                {"prompt": "Agent routines require a prompt."}
            )
        if kind == "command" and not attrs.get("command"):
            raise serializers.ValidationError(
                {"command": "Command routines require a command."}
            )
        schedule_type = attrs.get("scheduleType") or "daily"
        if schedule_type == "daily" and not attrs.get("time"):
            raise serializers.ValidationError(
                {"time": "Daily routines require a time."}
            )
        if schedule_type == "interval":
            attrs.setdefault("intervalSeconds", 60)
        return attrs


class TriggerFilterSerializer(serializers.Serializer):
    path = serializers.CharField(trim_whitespace=True, max_length=512)
    op = serializers.ChoiceField(
        choices=[
            "equals",
            "notEquals",
            "contains",
            "startsWith",
            "endsWith",
            "exists",
            "regex",
        ],
    )
    value = serializers.JSONField(required=False, allow_null=True)


class TriggerCreateSerializer(serializers.Serializer):
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=512,
    )
    hmacSecret = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=256,
    )
    hmacHeader = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=128,
    )
    senderPath = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=512,
    )
    senderAllowlist = serializers.ListField(
        child=serializers.CharField(trim_whitespace=True, max_length=256),
        required=False,
    )
    filters = TriggerFilterSerializer(many=True, required=False)
    relayEndpointId = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=64,
    )
    relayUrl = serializers.URLField(required=False, allow_blank=True, max_length=512)


class RoutineEmitSerializer(serializers.Serializer):
    payload = serializers.JSONField(required=False, allow_null=True)
    eventId = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=256,
    )


class RoutinesRunDueSerializer(serializers.Serializer):
    name = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=256,
    )
    force = serializers.BooleanField(required=False, default=False)


@api_view(["GET", "POST"])
def routines_list(request):
    """List or create persisted Super Agents routines."""
    manager = get_session_manager()
    if request.method == "POST":
        serializer = RoutineCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = _clean_serializer_data(dict(serializer.validated_data))
        try:
            result = async_to_sync(manager.save_routine)(payload)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_201_CREATED)

    routines = async_to_sync(manager.list_routines)()
    return Response(routines, status=status.HTTP_200_OK)


@api_view(["GET", "PATCH", "DELETE"])
def routine_detail(request, name):
    """Read, update, or delete one persisted Super Agents routine."""
    manager = get_session_manager()
    if request.method == "GET":
        try:
            result = async_to_sync(manager.read_routine)(name)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(result, status=status.HTTP_200_OK)

    if request.method == "DELETE":
        try:
            result = async_to_sync(manager.delete_routine)(name)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(result, status=status.HTTP_200_OK)

    serializer = RoutineSerializer(data={"name": name, **request.data}, partial=True)
    serializer.is_valid(raise_exception=True)
    payload = _clean_serializer_data(dict(serializer.validated_data))
    payload["name"] = name
    try:
        result = async_to_sync(manager.save_routine)(payload)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
def routines_run_due(request):
    """Run currently due routines through the local client library."""
    serializer = RoutinesRunDueSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    name = serializer.validated_data.get("name") or None
    force = bool(serializer.validated_data.get("force", False))
    manager = get_session_manager()
    try:
        result = async_to_sync(manager.run_due_routines)(name=name, force=force)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
def routine_triggers(request, name):
    """Add a webhook trigger to a persisted routine (loop)."""
    serializer = TriggerCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    payload = _clean_serializer_data(dict(serializer.validated_data))
    manager = get_session_manager()
    try:
        result = async_to_sync(manager.add_routine_trigger)(name, payload)
    except ValueError as exc:
        message = str(exc)
        not_found = "No Super Agents routine" in message
        return Response(
            {"error": message},
            status=status.HTTP_404_NOT_FOUND
            if not_found
            else status.HTTP_400_BAD_REQUEST,
        )
    token = result.get("trigger", {}).get("token")
    if token:
        result["ingestPath"] = f"/api/hooks/t/{token}/"
    return Response(result, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
def routine_trigger_detail(request, name, trigger_id):
    """Remove a trigger from a persisted routine (loop)."""
    manager = get_session_manager()
    try:
        result = async_to_sync(manager.remove_routine_trigger)(name, trigger_id)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
def routine_emit(request, name):
    """Run a loop immediately with a locally supplied event payload."""
    serializer = RoutineEmitSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    payload = serializer.validated_data.get("payload")
    event_id = serializer.validated_data.get("eventId") or None
    manager = get_session_manager()
    try:
        result = async_to_sync(manager.emit_routine_event)(name, payload, event_id)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def hooks_ingest(request, token):
    """Public ingest endpoint for webhook trigger deliveries.

    The URL token is the sole capability; per-trigger HMAC verification and
    sender allowlists are enforced by the delivery layer. Responses stay
    minimal so callers cannot probe loop configuration.
    """
    body = request.body or b""
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _INGEST_HEADER_DENYLIST
    }
    manager = get_session_manager()
    result = async_to_sync(manager.deliver_webhook_event)(
        token, headers=headers, body=body
    )
    delivery_status = result.get("status")
    if delivery_status == "unknown_token":
        return Response(status=status.HTTP_404_NOT_FOUND)
    if delivery_status == "rejected":
        if result.get("reason") == "payload_too_large":
            return Response(
                {"status": "rejected", "reason": "payload_too_large"},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        return Response({"status": "rejected"}, status=status.HTTP_403_FORBIDDEN)
    return Response(
        {"status": delivery_status, "eventId": result.get("eventId")},
        status=status.HTTP_200_OK,
    )
