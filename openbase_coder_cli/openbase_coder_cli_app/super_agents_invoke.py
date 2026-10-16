"""Run a Super Agents operation in this long-lived process.

The voice job forwards its Dispatcher's ``super_agents`` MCP calls here, so
agent turns started during a call are owned by django-cli and survive the
call ending (see ``openbase_coder_cli.super_agents_remote``).
"""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.openbase_coder_cli_app.thread_cache import (
    invalidate_thread_list_cache,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_errors import (
    thread_error_code,
    thread_error_message,
)
from openbase_coder_cli.super_agents_remote import (
    READ_ONLY_METHODS,
    invoke_super_agents_method,
)
from openbase_coder_cli.thread_sync.session_manager import get_session_manager

logger = logging.getLogger(__name__)


@api_view(["POST"])
def super_agents_invoke(request):
    payload = request.data if isinstance(request.data, dict) else {}
    method = payload.get("method")
    manager = get_session_manager()
    try:
        client = manager.super_agents_client_for_backend(payload.get("backend"))
        result = async_to_sync(invoke_super_agents_method)(client, payload)
    except ValueError as exc:
        return Response(
            {"error": str(exc), "code": "invalid", "type": "ValueError"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as exc:  # noqa: BLE001 - the caller gets the failure
        logger.warning("Super Agents %s failed in django-cli: %s", method, exc)
        return Response(
            {
                "error": thread_error_message(exc),
                "code": thread_error_code(exc, fallback="super_agents_failed"),
                "type": type(exc).__name__,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    if method not in READ_ONLY_METHODS:
        invalidate_thread_list_cache()
    return Response({"result": result})
