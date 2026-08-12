"""Routine management API views."""

from __future__ import annotations

from asgiref.sync import async_to_sync
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.thread_sync.session_manager import get_session_manager
from openbase_coder_cli.openbase_coder_cli_app.common import _clean_serializer_data


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
        if attrs.get("intervalSeconds") is not None and attrs.get("scheduleType") is None:
            attrs["scheduleType"] = "interval"
        return attrs


class RoutineCreateSerializer(RoutineSerializer):
    prompt = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
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
            raise serializers.ValidationError({"prompt": "Agent routines require a prompt."})
        if kind == "command" and not attrs.get("command"):
            raise serializers.ValidationError({"command": "Command routines require a command."})
        schedule_type = attrs.get("scheduleType") or "daily"
        if schedule_type == "daily" and not attrs.get("time"):
            raise serializers.ValidationError({"time": "Daily routines require a time."})
        if schedule_type == "interval":
            attrs.setdefault("intervalSeconds", 60)
        return attrs


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
