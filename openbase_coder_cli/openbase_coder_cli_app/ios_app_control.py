"""Foreground iOS app control API."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

IOS_APP_CONTROL_GROUP = "ios_app_control"
IOS_APP_CONTROL_ACK_TIMEOUT_SECONDS = 5.0
# Channel-layer group names only allow [a-zA-Z0-9._-]; command ids are
# validated against this before being embedded in an ack group name.
COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
IOS_APP_CONTROL_ACTIONS = {
    "open_url",
    "set_call_muted",
    "start_developer_call",
    "start_livekit_voice_test_call",
    "upload_diagnostics",
}
DISALLOWED_URL_SCHEMES = {"data", "file", "javascript"}
URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")


class IOSAppControlSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=sorted(IOS_APP_CONTROL_ACTIONS))
    url = serializers.CharField(
        required=False,
        trim_whitespace=True,
        max_length=4096,
    )
    muted = serializers.BooleanField(required=False)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=2000)

    def validate(self, attrs):
        action = attrs["action"]
        if action == "open_url":
            url = attrs.get("url", "")
            if not url:
                raise serializers.ValidationError("url is required for open_url.")
            _validate_url(url)
        elif action == "set_call_muted" and "muted" not in attrs:
            raise serializers.ValidationError("muted is required for set_call_muted.")
        return attrs


def _validate_url(value: str) -> None:
    parsed = urlparse(value)
    scheme = parsed.scheme
    if not scheme:
        raise serializers.ValidationError("url must include a scheme.")
    if not URL_SCHEME_RE.match(scheme):
        raise serializers.ValidationError("url has an invalid scheme.")
    if scheme.lower() in DISALLOWED_URL_SCHEMES:
        raise serializers.ValidationError(f"{scheme} URLs are not allowed.")
    if any(ord(char) < 32 for char in value):
        raise serializers.ValidationError("url must not contain control characters.")


def ack_group_name(command_id: str) -> str:
    return f"ios_app_control_ack.{command_id}"


async def _publish_and_await_ack(
    channel_layer, command: dict[str, Any], timeout: float
) -> bool:
    """Publish a command, then wait for a device ack on a per-command group.

    The ack channel is joined before publishing so the ack cannot race the
    subscription. Returns whether a connected app confirmed receipt.
    """
    ack_channel = await channel_layer.new_channel()
    ack_group = ack_group_name(command["command_id"])
    await channel_layer.group_add(ack_group, ack_channel)
    try:
        await channel_layer.group_send(
            IOS_APP_CONTROL_GROUP,
            {"type": "ios_app_control", "data": command},
        )
        try:
            await asyncio.wait_for(channel_layer.receive(ack_channel), timeout)
            return True
        except asyncio.TimeoutError:
            return False
    finally:
        await channel_layer.group_discard(ack_group, ack_channel)


def publish_ios_app_control(payload: dict[str, Any]) -> dict[str, Any]:
    command = {
        "command_id": f"ios-app-control-{uuid.uuid4().hex}",
        "created_at": time.time(),
        **payload,
    }
    channel_layer = get_channel_layer()
    if channel_layer is None:
        raise RuntimeError("Channel layer is not configured.")
    delivered = async_to_sync(_publish_and_await_ack)(
        channel_layer, command, IOS_APP_CONTROL_ACK_TIMEOUT_SECONDS
    )
    return {**command, "delivered": delivered}


@api_view(["POST"])
def ios_app_control(request):
    input_serializer = IOSAppControlSerializer(data=request.data)
    input_serializer.is_valid(raise_exception=True)
    try:
        command = publish_ios_app_control(dict(input_serializer.validated_data))
    except RuntimeError as exc:
        return Response(
            {"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE
        )
    return Response(
        {
            "command_id": command["command_id"],
            "status": "delivered" if command["delivered"] else "published",
            "delivered": command["delivered"],
            "action": command["action"],
        },
        status=status.HTTP_202_ACCEPTED,
    )
