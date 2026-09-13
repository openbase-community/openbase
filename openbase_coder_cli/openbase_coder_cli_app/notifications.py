"""Notification feed API views."""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.openbase_coder_cli_app import notification_store
from openbase_coder_cli.openbase_coder_cli_app.notification_producers import (
    sync_notification_producers,
)
from openbase_coder_cli.services.fleet_aggregation import (
    FLEET_SCOPE_PARAM,
    FLEET_SCOPE_VALUE,
    fleet_notifications,
)


class MarkReadSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True
    )
    kind = serializers.ChoiceField(
        choices=sorted(notification_store.VALID_KINDS), required=False
    )
    entity_id = serializers.CharField(required=False, allow_blank=False)

    def validate(self, attrs):
        if not attrs.get("ids") and not (attrs.get("kind") and attrs.get("entity_id")):
            raise serializers.ValidationError("Provide ids or both kind and entity_id.")
        return attrs


@api_view(["GET"])
def notification_list(request):
    """List notifications newest-first with the live unread count.

    Runs the producer sweep first so pure-polling clients (mobile apps)
    materialize report/approval/conflict notifications without any
    WebSocket client connected.
    """
    sync_notification_producers()
    include_read = request.query_params.get("include_read") != "false"
    try:
        limit = int(request.query_params.get("limit") or "")
    except ValueError:
        limit = notification_store.DEFAULT_LIST_LIMIT
    effective_limit = limit if limit > 0 else notification_store.DEFAULT_LIST_LIMIT
    payload = notification_store.list_notifications(
        include_read=include_read,
        limit=effective_limit,
    )
    if request.query_params.get(FLEET_SCOPE_PARAM) == FLEET_SCOPE_VALUE:
        # Notification stores are device-local; peer items carry origin_host
        # and are marked read by the client directly on the owning device.
        payload = fleet_notifications(
            payload, include_read=include_read, limit=effective_limit
        )
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["POST"])
def notification_mark_read(request):
    """Mark notifications read by id list or by (kind, entity_id)."""
    serializer = MarkReadSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    marked = notification_store.mark_read(
        data.get("ids"),
        kind=data.get("kind"),
        entity_id=data.get("entity_id"),
    )
    return Response({"marked": marked}, status=status.HTTP_200_OK)


@api_view(["POST"])
def notification_mark_all_read(request):
    return Response(
        {"marked": notification_store.mark_all_read()},
        status=status.HTTP_200_OK,
    )
