"""Approval request API views."""

from __future__ import annotations

from asgiref.sync import async_to_sync
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.skill_approvals import (
    answer_skill_approval_request,
    consume_skill_approval_decision,
    create_skill_approval_request,
    get_skill_approval_decision,
    get_skill_approval_request,
    is_pending_skill_approval_request,
    is_skill_approval_request,
)
from openbase_coder_cli.thread_sync.session_manager import get_session_manager


class ApprovalRequestActionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["accept", "decline", "cancel"])


class SkillApprovalRequestCreateSerializer(serializers.Serializer):
    skill = serializers.CharField()
    action = serializers.CharField()
    description = serializers.CharField()
    command = serializers.CharField(required=False, allow_blank=True)
    details = serializers.JSONField(required=False)
    timeout_seconds = serializers.FloatField(required=False, min_value=0)


async def pending_approval_requests() -> list[dict]:
    """Return pending thread and skill approvals from the native queue."""
    manager = get_session_manager()
    return [
        approval_request
        for approval_request in await manager.list_approval_requests()
        if not is_skill_approval_request(approval_request)
        or is_pending_skill_approval_request(approval_request)
    ]


@api_view(["GET"])
def approval_requests(request):
    """List currently pending approval requests across threads and skills."""
    requests = async_to_sync(pending_approval_requests)()
    return Response({"requests": requests}, status=status.HTTP_200_OK)


@api_view(["POST"])
def approval_request_detail(request, request_id):
    """Approve or deny one pending approval request."""
    serializer = ApprovalRequestActionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    decision = serializer.validated_data["decision"]
    try:
        result = answer_skill_approval_request(request_id, decision)
    except ValueError:
        result = None
    if result is not None:
        return Response({"success": True, "result": result}, status=status.HTTP_200_OK)

    manager = get_session_manager()
    try:
        result = async_to_sync(manager.answer_approval_request)(
            request_id,
            decision,
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    return Response({"success": True, "result": result}, status=status.HTTP_200_OK)


@api_view(["POST"])
def skill_approval_requests(request):
    """Create a skill-originated approval request."""
    serializer = SkillApprovalRequestCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    details = serializer.validated_data.get("details") or {}
    if not isinstance(details, dict):
        return Response(
            {"error": "details must be an object"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        approval_request = create_skill_approval_request(
            skill=serializer.validated_data["skill"],
            action=serializer.validated_data["action"],
            description=serializer.validated_data["description"],
            details=details,
            command=serializer.validated_data.get("command") or None,
            timeout_seconds=serializer.validated_data.get("timeout_seconds"),
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"request": approval_request}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
def skill_approval_request_detail(request, request_id):
    """Return pending or answered state for one skill approval request."""
    approval_request = get_skill_approval_request(request_id)
    decision = get_skill_approval_decision(request_id)
    if approval_request is None and decision is None:
        return Response(
            {"error": f"approval request not found: {request_id}"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(
        {"request": approval_request, "decision": decision},
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
def skill_approval_request_consume(request, request_id):
    """Consume an answered skill approval decision after the caller observes it."""
    try:
        decision = consume_skill_approval_decision(request_id)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    return Response({"decision": decision}, status=status.HTTP_200_OK)
