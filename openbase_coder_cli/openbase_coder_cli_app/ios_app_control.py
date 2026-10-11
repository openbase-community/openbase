"""Foreground iOS app control API."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.login_callback import (
    DEFAULT_FORWARD_TTL_SECONDS,
    is_tailnet_forward_target,
)
from openbase_coder_cli.open_url_policy import normalize_open_url, open_url_error

IOS_APP_CONTROL_GROUP = "ios_app_control"
logger = logging.getLogger(__name__)
IOS_APP_CONTROL_ACK_TIMEOUT_SECONDS = 5.0
# An open that asks the phone to forward a login callback acks only after the
# phone bound its loopback listener and health-checked the relay (up to four
# seconds); timing out first makes the caller push a second copy of the page.
IOS_FORWARDED_OPEN_ACK_TIMEOUT_SECONDS = 10.0
# Channel-layer group names only allow [a-zA-Z0-9._-]; command ids are
# validated against this before being embedded in an ack group name.
COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Bounds for the loopback forward a phone may be asked to run for a login.
FORWARD_MIN_PORT = 1024
FORWARD_MAX_PORT = 65535
FORWARD_MAX_TTL_SECONDS = DEFAULT_FORWARD_TTL_SECONDS
FORWARD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
FORWARD_TARGET_RE = re.compile(r"^[A-Za-z0-9.:\[\]-]{1,253}$")
IOS_CALL_CONTROL_ACTIONS = {"set_speaker", "end_call", "start_call"}
IOS_CALL_CONTROL_ACK_TIMEOUT_SECONDS = 45.0
# A short code the user has to type on a sign-in page (device code), put on
# the phone's clipboard. Never logged; the app keeps it local and expiring.
COPY_TEXT_MAX_LENGTH = 256
COPY_LABEL_MAX_LENGTH = 60
# Text shown on the phone in a large, easily selectable sheet (a sign-in code
# or a short instruction); same one-line rules as copy_text, a little longer.
SHOW_TEXT_MAX_LENGTH = 512
TEXT_ACTION_LIMITS = {
    "copy_text": COPY_TEXT_MAX_LENGTH,
    "show_text": SHOW_TEXT_MAX_LENGTH,
}
APP_CONTROL_ACTIONS = IOS_CALL_CONTROL_ACTIONS | {
    "copy_text",
    "show_text",
    "open_url",
    "set_call_muted",
    "start_developer_call",
    "start_livekit_voice_test_call",
    "upload_diagnostics",
}
IOS_APP_CONTROL_ACTIONS = APP_CONTROL_ACTIONS


class LoopbackForwardSerializer(serializers.Serializer):
    """Ask the phone to forward its loopback ``port`` to ``target:port``.

    Carried with ``open_url`` for CLI logins whose redirect points at
    localhost; the phone keeps the forward for ``ttl_seconds`` at most.
    """

    port = serializers.IntegerField(
        min_value=FORWARD_MIN_PORT, max_value=FORWARD_MAX_PORT
    )
    target = serializers.RegexField(FORWARD_TARGET_RE, max_length=253)
    ttl_seconds = serializers.IntegerField(
        min_value=1, max_value=FORWARD_MAX_TTL_SECONDS
    )
    token = serializers.RegexField(FORWARD_TOKEN_RE, max_length=128)

    def validate_target(self, value):
        if not is_tailnet_forward_target(value):
            raise serializers.ValidationError("target must be a literal VPN address.")
        return value


class IOSAppControlSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=sorted(APP_CONTROL_ACTIONS))
    loopback_forward = LoopbackForwardSerializer(required=False)
    url = serializers.CharField(
        required=False,
        trim_whitespace=True,
        max_length=4096,
    )
    muted = serializers.BooleanField(required=False)
    speaker = serializers.BooleanField(required=False)
    thread_id = serializers.CharField(
        required=False, max_length=256, trim_whitespace=True
    )
    limit = serializers.IntegerField(required=False, min_value=1, max_value=2000)
    text = serializers.CharField(
        required=False, max_length=SHOW_TEXT_MAX_LENGTH, trim_whitespace=False
    )
    label = serializers.CharField(
        required=False, max_length=COPY_LABEL_MAX_LENGTH, trim_whitespace=True
    )

    def validate(self, attrs):
        action = attrs["action"]
        if action in TEXT_ACTION_LIMITS:
            text = attrs.get("text", "")
            if not text or any(ord(char) < 32 or ord(char) == 127 for char in text):
                raise serializers.ValidationError(
                    f"text is required for {action} and must be one line."
                )
            if len(text) > TEXT_ACTION_LIMITS[action]:
                raise serializers.ValidationError(
                    f"text for {action} is at most {TEXT_ACTION_LIMITS[action]} characters."
                )
            if attrs.get("url"):
                _validate_url(attrs["url"])
                attrs["url"] = normalize_open_url(attrs["url"])
            if "loopback_forward" in attrs:
                raise serializers.ValidationError(
                    "loopback_forward only applies to open_url."
                )
            return attrs
        if "text" in attrs or "label" in attrs:
            raise serializers.ValidationError(
                "text and label apply to copy_text and show_text only."
            )
        if action == "open_url":
            url = attrs.get("url", "")
            if not url:
                raise serializers.ValidationError("url is required for open_url.")
            _validate_url(url)
            attrs["url"] = normalize_open_url(url)
        elif "loopback_forward" in attrs:
            raise serializers.ValidationError(
                "loopback_forward only applies to open_url."
            )
        elif action == "set_call_muted" and "muted" not in attrs:
            raise serializers.ValidationError("muted is required for set_call_muted.")
        elif action == "set_speaker" and "speaker" not in attrs:
            raise serializers.ValidationError("speaker is required for set_speaker.")
        elif action == "start_call" and not attrs.get("thread_id"):
            raise serializers.ValidationError(
                "thread_id is required for start_call (or dispatcher)."
            )
        return attrs


def _validate_url(value: str) -> None:
    error = open_url_error(value)
    if error:
        raise serializers.ValidationError(error)


def ack_group_name(command_id: str) -> str:
    return f"ios_app_control_ack.{command_id}"


async def _publish_and_await_ack(
    channel_layer, command: dict[str, Any], timeout: float
) -> dict[str, Any] | None:
    """Publish a command, then wait for a device ack on a per-command group.

    The ack channel is joined before publishing so the ack cannot race the
    subscription. Returns the device acknowledgement, or None on timeout.
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
            return await asyncio.wait_for(channel_layer.receive(ack_channel), timeout)
        except asyncio.TimeoutError:
            return None
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
    is_call_control = command["action"] in IOS_CALL_CONTROL_ACTIONS
    if is_call_control:
        ack_timeout = IOS_CALL_CONTROL_ACK_TIMEOUT_SECONDS
    elif command.get("loopback_forward"):
        ack_timeout = IOS_FORWARDED_OPEN_ACK_TIMEOUT_SECONDS
    else:
        ack_timeout = IOS_APP_CONTROL_ACK_TIMEOUT_SECONDS
    ack = async_to_sync(_publish_and_await_ack)(channel_layer, command, ack_timeout)
    delivered = ack is not None
    result = {}
    if ack is not None and type(ack.get("shown")) is bool:
        result["shown"] = ack["shown"]
        if type(ack.get("notified")) is bool:
            result["notified"] = ack["notified"]
    elif ack is not None and type(ack.get("copied")) is bool:
        result["copied"] = ack["copied"]
        if type(ack.get("opened")) is bool:
            result["opened"] = ack["opened"]
    elif ack is not None and type(ack.get("opened")) is bool:
        # Newer apps ack after the open attempt and report its outcome.
        result["opened"] = ack["opened"]
        if type(ack.get("notified")) is bool:
            result["notified"] = ack["notified"]
        if isinstance(ack.get("forward"), str):
            result["forward"] = ack["forward"]
            if isinstance(ack.get("forward_error"), str):
                result["forward_error"] = ack["forward_error"]
        if isinstance(ack.get("error"), str):
            result["error"] = ack["error"]
    if is_call_control:
        result = {"applied": False}
        if ack is not None:
            result.update(
                {
                    key: ack[key]
                    for key in ("applied", "call_state", "error")
                    if key in ack
                }
            )
            if "call_state" not in result:
                result.update(
                    applied=False,
                    error="Device did not return call state; update the iOS app.",
                )
    logger.info(
        "dispatch_timing stage=ios_control_round_trip command_id=%s "
        "server_sent_unix_ms=%.3f server_ack_unix_ms=%.3f delivered=%s",
        command["command_id"],
        command["created_at"] * 1000,
        time.time() * 1000,
        delivered,
    )
    return {**command, "delivered": delivered, **result}


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
            **{
                key: command[key]
                for key in (
                    "applied",
                    "call_state",
                    "error",
                    "copied",
                    "shown",
                    "opened",
                    "notified",
                    "forward",
                    "forward_error",
                )
                if key in command
            },
        },
        status=status.HTTP_202_ACCEPTED,
    )
