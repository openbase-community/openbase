"""API for pushing threads to a durable machine (see ``services.thread_push``).

Sending side, called by the console, desktop and ``openbase-coder threads
push``:

- ``GET  threads/<id>/push/``          where the thread can go, and why not
- ``POST threads/<id>/push/``          push it (``to``, ``message``, ``request_id``)
- ``POST threads/<id>/push/cancel/``   make an unfinished push usable here again
- ``POST threads/<id>/push/release/``  make a moved thread's copy usable here

Receiving side, called peer to peer by the sending computer's runtime:

- ``GET  threads/push/target/``             what this computer can receive
- ``POST threads/push/arrivals/``           take a pushed thread
- ``GET  threads/push/arrivals/<op id>/``   outcome of an earlier arrival
"""

from __future__ import annotations

from asgiref.sync import async_to_sync
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.openbase_coder_cli_app.common import ExactFieldsSerializer
from openbase_coder_cli.openbase_coder_cli_app.thread_cache import (
    invalidate_thread_list_cache,
)
from openbase_coder_cli.services import thread_push
from openbase_coder_cli.services.thread_push import PushError
from openbase_coder_cli.thread_sync.session_manager import get_session_manager


class PushInput(ExactFieldsSerializer):
    to = serializers.CharField(required=False, allow_blank=True, max_length=255)
    message = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=False,
        max_length=thread_push.MAX_MESSAGE_CHARS,
    )
    request_id = serializers.UUIDField(required=False)


class CancelInput(ExactFieldsSerializer):
    force = serializers.BooleanField(required=False, default=False)


def _error(exc: PushError) -> Response:
    return Response(exc.to_json(), status=exc.http_status)


@api_view(["GET", "POST"])
def thread_push(request, thread_id):
    manager = get_session_manager()
    if request.method == "GET":
        try:
            return Response(async_to_sync(thread_push.push_options)(manager, thread_id))
        except PushError as exc:
            return _error(exc)
    data = PushInput(data=request.data if isinstance(request.data, dict) else {})
    data.is_valid(raise_exception=True)
    request_id = data.validated_data.get("request_id")
    try:
        result = async_to_sync(thread_push.push_thread)(
            manager,
            thread_id,
            to=data.validated_data.get("to") or None,
            message=data.validated_data.get("message") or None,
            request_id=str(request_id) if request_id else None,
        )
    except PushError as exc:
        invalidate_thread_list_cache()
        return _error(exc)
    invalidate_thread_list_cache()
    return Response(result)


@api_view(["POST"])
def thread_push_cancel(request, thread_id):
    data = CancelInput(data=request.data if isinstance(request.data, dict) else {})
    data.is_valid(raise_exception=True)
    try:
        result = async_to_sync(thread_push.cancel_push)(
            thread_id, force=data.validated_data["force"]
        )
    except PushError as exc:
        return _error(exc)
    invalidate_thread_list_cache()
    return Response(result)


@api_view(["POST"])
def thread_push_release(request, thread_id):
    result = thread_push.release_moved(thread_id)
    invalidate_thread_list_cache()
    return Response(result)


@api_view(["GET"])
def thread_push_target(request):
    return Response(thread_push.target_capabilities())


@api_view(["POST"])
def thread_push_arrivals(request):
    payload = request.data if isinstance(request.data, dict) else {}
    try:
        result = async_to_sync(thread_push.accept_push)(get_session_manager(), payload)
    except PushError as exc:
        return _error(exc)
    return Response(result)


@api_view(["GET"])
def thread_push_arrival_detail(request, operation_id):
    try:
        result = thread_push.arrival_status(operation_id)
    except PushError as exc:
        return _error(exc)
    if result is None:
        return Response(
            {"error": "Unknown push operation.", "code": "not_found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(result)
