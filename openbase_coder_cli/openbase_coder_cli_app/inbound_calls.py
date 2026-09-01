"""Authenticated local broker for explicit inbound iPhone calls."""

from __future__ import annotations

import asyncio
import unicodedata

from asgiref.sync import async_to_sync
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.config.cloud_inbound_calls import (
    InboundCallCloudError,
    request_inbound_call_ring,
)
from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
)
from openbase_coder_cli.inbound_calls import (
    InboundCallInvitationConflict,
    InboundCallInvitationError,
    InboundCallInvitationExpired,
    create_invitation,
    decline_invitation,
    invitation_for_activation,
    mark_invitation_activated,
    new_invitation_id,
    remove_pending_invitation,
    set_cloud_expiry,
)
from openbase_coder_cli.livekit_agent.config import LIVEKIT_DISPATCH_AGENT_NAME
from openbase_coder_cli.livekit_announcer import NoActiveLiveKitRoomError
from openbase_coder_cli.livekit_voice_history import (
    AgentVoiceLookupError,
    get_voice_history_entry_for_agent_name,
)
from openbase_coder_cli.livekit_voice_route import publish_transfer_to_thread
from openbase_coder_cli.openbase_coder_cli_app.common import (
    ExactFieldsSerializer,
    _request_identity,
)

ROUTE_ACTIVATION_ATTEMPTS = 24
ROUTE_ACTIVATION_RETRY_SECONDS = 0.5


def _validate_display_name(value: str) -> str:
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise serializers.ValidationError("Control characters are not allowed.")
    normalized = " ".join(value.split())
    if not normalized:
        raise serializers.ValidationError("This field may not be blank.")
    return normalized


class UserCallSerializer(ExactFieldsSerializer):
    agent_name = serializers.CharField(max_length=80, trim_whitespace=True)
    caller_name = serializers.CharField(
        max_length=80,
        trim_whitespace=True,
        required=False,
        allow_blank=True,
    )

    def validate_agent_name(self, value: str) -> str:
        return _validate_display_name(value)

    def validate_caller_name(self, value: str) -> str:
        return _validate_display_name(value) if value else ""


class InboundCallActionSerializer(ExactFieldsSerializer):
    inbound_invitation_id = serializers.RegexField(r"^[A-Za-z0-9_-]{43}$")


class InboundCallActivateSerializer(InboundCallActionSerializer):
    room_name = serializers.RegexField(r"^room-inbound-[0-9a-f]{24}$")


@api_view(["POST"])
def user_call(request):
    """Ring the user's registered iPhones for one known local agent thread."""
    serializer = UserCallSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    requested_name = serializer.validated_data["agent_name"]
    try:
        voice_entry = get_voice_history_entry_for_agent_name(requested_name)
    except AgentVoiceLookupError as exc:
        return Response(
            {"detail": str(exc), "code": "agent_not_found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    if not voice_entry.thread_id or not voice_entry.cwd:
        return Response(
            {
                "detail": "The selected agent has no resumable working directory.",
                "code": "agent_not_resumable",
            },
            status=status.HTTP_409_CONFLICT,
        )

    identity = _request_identity(request)
    invitation_id = new_invitation_id()
    caller_name = (
        serializer.validated_data.get("caller_name")
        or voice_entry.agent_name
        or requested_name
    )
    invitation = create_invitation(
        invitation_id=invitation_id,
        account_identity=identity,
        caller_name=caller_name,
        thread_id=voice_entry.thread_id,
        cwd=voice_entry.cwd,
        agent_name=voice_entry.agent_name or requested_name,
        livekit_dispatch_agent_name=LIVEKIT_DISPATCH_AGENT_NAME,
    )
    try:
        acceptance = request_inbound_call_ring(
            invitation_id=invitation_id,
            caller_name=caller_name,
            access_token=_request_bearer_jwt(request),
        )
        invitation = set_cloud_expiry(
            invitation_id,
            account_identity=identity,
            expires_at=acceptance.expires_at,
        )
    except AuthLoginRequiredError as exc:
        remove_pending_invitation(invitation_id, account_identity=identity)
        return Response(
            {"detail": str(exc), "code": "login_required"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    except (AuthTransientError, InboundCallInvitationConflict) as exc:
        remove_pending_invitation(invitation_id, account_identity=identity)
        return Response(
            {"detail": str(exc), "code": "cloud_unavailable"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except InboundCallCloudError as exc:
        remove_pending_invitation(invitation_id, account_identity=identity)
        return Response(
            {"detail": str(exc), "code": "call_rejected"},
            status=status.HTTP_409_CONFLICT,
        )

    return Response(
        {
            "invitation_id": invitation.invitation_id,
            "expires_at": int(invitation.expires_at),
            "device_count": acceptance.device_count,
            "agent_name": invitation.agent_name,
        },
        status=status.HTTP_202_ACCEPTED,
    )


@api_view(["POST"])
def inbound_call_activate(request):
    """Activate the stored thread route after the answered room connects."""
    serializer = InboundCallActivateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    invitation_id = serializer.validated_data["inbound_invitation_id"]
    room_name = serializer.validated_data["room_name"]
    identity = _request_identity(request)
    try:
        invitation = invitation_for_activation(
            invitation_id,
            account_identity=identity,
            room_name=room_name,
        )
        if invitation.status != "activated":
            async_to_sync(_activate_route)(invitation)
            invitation = mark_invitation_activated(
                invitation_id,
                account_identity=identity,
            )
    except InboundCallInvitationExpired as exc:
        return _invitation_error(exc, status.HTTP_410_GONE, "invitation_expired")
    except InboundCallInvitationConflict as exc:
        return _invitation_error(exc, status.HTTP_409_CONFLICT, "invitation_conflict")
    except InboundCallInvitationError as exc:
        return _invitation_error(exc, status.HTTP_404_NOT_FOUND, "invitation_not_found")
    except NoActiveLiveKitRoomError as exc:
        return Response(
            {"detail": str(exc), "code": "room_not_ready"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response({"status": invitation.status, "room_name": invitation.room_name})


async def _activate_route(invitation) -> None:
    for attempt in range(ROUTE_ACTIVATION_ATTEMPTS):
        try:
            await publish_transfer_to_thread(
                invitation.thread_id,
                directory=invitation.cwd,
                label=invitation.agent_name,
                agent_name=invitation.agent_name,
                room_name=invitation.room_name,
            )
            return
        except NoActiveLiveKitRoomError:
            if attempt + 1 == ROUTE_ACTIVATION_ATTEMPTS:
                raise
            await asyncio.sleep(ROUTE_ACTIVATION_RETRY_SECONDS)


@api_view(["POST"])
def inbound_call_decline(request):
    """Invalidate an invitation when CallKit declines or ends before answer."""
    serializer = InboundCallActionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        invitation = decline_invitation(
            serializer.validated_data["inbound_invitation_id"],
            account_identity=_request_identity(request),
        )
    except InboundCallInvitationExpired as exc:
        return _invitation_error(exc, status.HTTP_410_GONE, "invitation_expired")
    except InboundCallInvitationConflict as exc:
        return _invitation_error(exc, status.HTTP_409_CONFLICT, "invitation_conflict")
    except InboundCallInvitationError as exc:
        return _invitation_error(exc, status.HTTP_404_NOT_FOUND, "invitation_not_found")
    return Response({"status": invitation.status})


def _request_bearer_jwt(request) -> str | None:
    authorization = request.META.get("HTTP_AUTHORIZATION", "")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].casefold() != "bearer":
        return None
    token = parts[1].strip()
    return token if token.count(".") == 2 else None


def _invitation_error(exc: Exception, response_status: int, code: str) -> Response:
    return Response({"detail": str(exc), "code": code}, status=response_status)
