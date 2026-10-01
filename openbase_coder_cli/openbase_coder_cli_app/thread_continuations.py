"""Authenticated API for linked backend continuations."""

from asgiref.sync import async_to_sync
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.thread_sync import continuation_store
from openbase_coder_cli.thread_sync.continuations import create_continuation, options
from openbase_coder_cli.thread_sync.session_manager import get_session_manager

from .common import ExactFieldsSerializer
from .thread_cache import invalidate_thread_list_cache
from .thread_errors import thread_error_message


class ContinuationInput(ExactFieldsSerializer):
    backend = serializers.ChoiceField(choices=["codex", "claude_code"])
    request_id = serializers.UUIDField()


@api_view(["GET"])
def thread_continuation_options(request, thread_id):
    try:
        return Response(async_to_sync(options)(get_session_manager(), thread_id))
    except (ValueError, RuntimeError) as exc:
        return Response({"error": thread_error_message(exc)}, status=409)


@api_view(["POST"])
def thread_continuations(request, thread_id):
    data = ContinuationInput(data=request.data)
    data.is_valid(raise_exception=True)
    try:
        result = async_to_sync(create_continuation)(
            get_session_manager(),
            thread_id,
            data.validated_data["backend"],
            str(data.validated_data["request_id"]),
        )
    except (ValueError, RuntimeError, OSError) as exc:
        record = continuation_store.for_operation(
            str(data.validated_data["request_id"])
        )
        return Response(
            {
                "error": thread_error_message(exc),
                "safe_to_retry": record is None or record.get("safe_to_retry", False),
            },
            status=409,
        )
    invalidate_thread_list_cache()
    return Response(result, status=201)


@api_view(["GET"])
def thread_continuation_context(request, thread_id):
    record = continuation_store.for_destination(thread_id)
    if record is None or record["state"] != "ready":
        return Response({"error": "Continuation not found."}, status=404)
    return Response(
        {
            "context": record["context"],
            "messages": record["messages"],
            "omitted": record["omitted"],
        }
    )
