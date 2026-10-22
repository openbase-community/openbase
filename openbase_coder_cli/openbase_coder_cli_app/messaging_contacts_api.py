"""API for categorizing personal-messaging contacts (WhatsApp, iMessage)."""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.messaging_contacts import service


class AssignSerializer(serializers.Serializer):
    keys = serializers.ListField(child=serializers.CharField(), allow_empty=False)
    category = serializers.CharField(allow_null=True, allow_blank=False)


class SkipSerializer(serializers.Serializer):
    keys = serializers.ListField(child=serializers.CharField(), allow_empty=False)
    hours = serializers.FloatField(required=False, min_value=0)


class CategorySerializer(serializers.Serializer):
    label = serializers.CharField()
    policy = serializers.ChoiceField(choices=["ask", "block"], default="ask")


@api_view(["GET"])
def messaging_contacts(request):
    """People with messages in the last year, their categories and the queue.

    ``?refresh=1`` re-reads the sources; otherwise a cache newer than the
    index TTL is served. ``?cache_only=1`` never scans (badge polling).
    """
    refresh = request.query_params.get("refresh") in {"1", "true"}
    cache_only = request.query_params.get("cache_only") in {"1", "true"}
    return Response(service.snapshot(refresh=refresh, cache_only=cache_only))


@api_view(["GET"])
def messaging_contacts_summary(request):
    return Response(service.summary())


@api_view(["POST"])
def messaging_contacts_assign(request):
    serializer = AssignSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        result = service.assign(
            serializer.validated_data["keys"], serializer.validated_data["category"]
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({**result, "progress": service.summary()["progress"]})


@api_view(["POST"])
def messaging_contacts_skip(request):
    serializer = SkipSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    hours = serializer.validated_data.get("hours", service.SKIP_HOURS)
    try:
        result = service.skip(serializer.validated_data["keys"], hours)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({**result, "progress": service.summary()["progress"]})


@api_view(["GET", "POST"])
def messaging_contact_categories(request):
    if request.method == "GET":
        return Response({"categories": service.list_categories()})
    serializer = CategorySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        category = service.save_category(
            serializer.validated_data["label"], serializer.validated_data["policy"]
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(
        {"category": category, "categories": service.list_categories()},
        status=status.HTTP_201_CREATED,
    )


@api_view(["PATCH", "DELETE"])
def messaging_contact_category_detail(request, category_id):
    try:
        if request.method == "DELETE":
            result = service.delete_category(category_id)
            return Response({**result, "categories": service.list_categories()})
        serializer = CategorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        category = service.save_category(
            serializer.validated_data["label"],
            serializer.validated_data["policy"],
            category_id,
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    return Response({"category": category, "categories": service.list_categories()})


@api_view(["GET"])
def messaging_contact_lookup(request):
    """Category state for one source contact; used by agents and skills."""
    kind = str(request.query_params.get("kind") or "").strip().lower()
    contact_id = str(request.query_params.get("contact_id") or "").strip()
    if not kind or not contact_id:
        return Response(
            {"error": "kind and contact_id are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    row = service.lookup_contact(kind, contact_id)
    if row is None:
        return Response(
            {"error": "contact not found in the cached index"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response({"contact": row})
