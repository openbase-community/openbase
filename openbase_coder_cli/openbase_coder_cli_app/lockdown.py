"""Authenticated, non-secret voice-lockdown status and challenge API."""

from __future__ import annotations

from asgiref.sync import async_to_sync
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from super_agents.approval_redaction import redact_approval_payload

from openbase_coder_cli.thread_sync.session_manager import get_session_manager
from openbase_coder_cli.voice_lockdown.broker import (
    ApprovalScope,
    LockdownDeniedError,
    canonical_action_digest,
    get_voice_lockdown_broker,
)


class LockdownChallengeSerializer(serializers.Serializer):
    requestId = serializers.CharField(max_length=256)
    roomSid = serializers.CharField(max_length=256)
    participantIdentity = serializers.CharField(max_length=256)


@api_view(["GET"])
def lockdown_status(request):
    """Return only non-secret health, baseline, and challenge metadata."""
    return Response(get_voice_lockdown_broker().status(), status=status.HTTP_200_OK)


@api_view(["POST"])
def lockdown_challenges(request):
    """Arm phrase capture for one authoritative pending approval."""
    serializer = LockdownChallengeSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    request_id = serializer.validated_data["requestId"]
    manager = get_session_manager()
    pending = async_to_sync(manager.list_approval_requests)()
    approval = next((item for item in pending if str(item.get("id")) == request_id), None)
    if approval is None:
        return Response({"error": "The approval request is not pending."}, status=status.HTTP_404_NOT_FOUND)
    params = approval.get("params") if isinstance(approval.get("params"), dict) else {}
    tool_name = str(params.get("toolName") or params.get("name") or approval.get("method") or "")
    action_input = {
        key: value
        for key, value in params.items()
        if key
        not in {
            "approvalExpiresAt",
            "approvalActionDigest",
            "approvalManagedBy",
            "approvalOwnerId",
            "approvalOwnerPid",
            "backend",
            "description",
            "name",
            "threadId",
            "title",
            "toolCallId",
            "toolName",
            "turnId",
        }
    }
    action = {
        "method": str(approval.get("method") or ""),
        "tool": tool_name,
        "input": action_input,
    }
    scope = ApprovalScope(
        backend=str(params.get("backend") or "codex"),
        request_id=request_id,
        thread_id=str(params.get("threadId") or ""),
        turn_id=str(params.get("turnId") or ""),
        tool_call_id=str(params.get("toolCallId") or params.get("itemId") or ""),
        tool_name=tool_name,
        action_digest=str(params.get("approvalActionDigest") or canonical_action_digest(action)),
        room_sid=serializer.validated_data["roomSid"],
        participant_identity=serializer.validated_data["participantIdentity"],
    )
    try:
        challenge = get_voice_lockdown_broker().create_challenge(
            scope,
            action_summary=str(
                redact_approval_payload(params.get("description") or params.get("name") or tool_name)
            ),
        )
    except LockdownDeniedError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
    return Response({"challenge": challenge}, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
def lockdown_challenge_detail(request, challenge_id):
    """Cancel one challenge; there is intentionally no phrase submission API."""
    try:
        cancelled = get_voice_lockdown_broker().cancel_challenge(challenge_id)
    except LockdownDeniedError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
    if not cancelled:
        return Response({"error": "Challenge is no longer pending."}, status=status.HTTP_404_NOT_FOUND)
    return Response({"cancelled": True}, status=status.HTTP_200_OK)
