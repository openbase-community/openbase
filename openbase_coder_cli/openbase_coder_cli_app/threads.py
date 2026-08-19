"""Thread control API views."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from asgiref.sync import async_to_sync
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.livekit_voice_route import (
    get_livekit_voice_route_state,
    warm_livekit_dispatcher_thread,
)
from openbase_coder_cli.openbase_coder_cli_app.common import _auth_debug_value
from openbase_coder_cli.openbase_coder_cli_app.item_tags import (
    set_thread_tags,
    thread_tags_payload,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_cache import (
    get_cached_thread_list,
    get_cached_thread_page,
    get_cached_thread_state,
    invalidate_thread_list_cache,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_errors import (
    THREAD_DATA_UNAVAILABLE_CODE,
    is_thread_data_unavailable_error,
    thread_error_message,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_favorites import (
    favorite_payload,
    is_thread_favorite,
    set_thread_favorite,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_metadata import (
    annotate_thread_payload,
    get_livekit_shared_thread_id,
)
from openbase_coder_cli.thread_sync.models import ThreadStatus
from openbase_coder_cli.thread_sync.projects import (
    refresh_projects_from_thread_directories as _refresh_projects_from_threads,
)
from openbase_coder_cli.thread_sync.session_manager import (
    ThreadListPage,
    get_session_manager,
)

logger = logging.getLogger(__name__)

DEFAULT_THREAD_PAGE_SIZE = 25
MAX_THREAD_PAGE_SIZE = 100
RUN_ACTIVITY_FRESHNESS = timedelta(minutes=5)
DISPATCHER_THREAD_WARM_TIMEOUT_SECONDS = 20.0


def _parse_positive_int(
    value: Any, *, name: str, default: int
) -> tuple[int | None, str | None]:
    if value is None or value == "":
        return default, None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, f"{name} must be a positive integer"
    if parsed < 1:
        return None, f"{name} must be a positive integer"
    return parsed, None


def _parse_optional_bool(value: Any, *, name: str) -> tuple[bool | None, str | None]:
    if value is None or value == "":
        return None, None
    if isinstance(value, bool):
        return value, None
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes"}:
        return True, None
    if normalized in {"0", "false", "no"}:
        return False, None
    return None, f"{name} must be true or false"


def _thread_page_url(request, *, page: int, page_size: int) -> str:
    query = request.query_params.copy()
    query["page"] = str(page)
    query["page_size"] = str(page_size)
    query.pop("cursor", None)
    return f"{request.path}?{query.urlencode()}"


def _offset_thread_page_url(request, *, page: int, page_size: int) -> str:
    query = request.query_params.copy()
    query["page"] = str(page)
    query["page_size"] = str(page_size)
    query.pop("cursor", None)
    return f"{request.path}?{query.urlencode()}"


def _thread_cursor_url(
    request, *, page: int, page_size: int, cursor: str | None
) -> str:
    query = request.query_params.copy()
    query["page"] = str(page)
    query["page_size"] = str(page_size)
    if cursor:
        query["cursor"] = cursor
    else:
        query.pop("cursor", None)
    return f"{request.path}?{query.urlencode()}"


def _get_thread_page_result(
    manager,
    *,
    page: int,
    page_size: int,
    cursor: str | None,
) -> ThreadListPage:
    if cursor or page == 1:
        return get_cached_thread_page(manager, limit=page_size, cursor=cursor)

    next_cursor: str | None = None
    for _ in range(1, page):
        previous_page = get_cached_thread_page(
            manager,
            limit=page_size,
            cursor=next_cursor,
        )
        next_cursor = previous_page.next_cursor
        if next_cursor is None:
            return ThreadListPage(threads=[], next_cursor=None)
    return get_cached_thread_page(manager, limit=page_size, cursor=next_cursor)


def _thread_sort_value(thread):
    return (
        thread.current_run.started_at
        if thread.current_run is not None
        else thread.updated_at
    )


def _include_livekit_fallback_thread(manager, threads: list) -> list:
    livekit_thread_id = get_livekit_shared_thread_id()
    if not livekit_thread_id or any(
        thread.session_id == livekit_thread_id for thread in threads
    ):
        return threads
    livekit_thread = _get_cached_livekit_dispatcher_thread(manager)
    if livekit_thread is None:
        return threads
    logger.info(
        "thread_list adding LiveKit dispatcher fallback thread_id=%s",
        livekit_thread_id,
    )
    return sorted([*threads, livekit_thread], key=_thread_sort_value, reverse=True)


def _get_cached_livekit_dispatcher_thread(manager):
    livekit_thread_id = get_livekit_shared_thread_id()
    if not livekit_thread_id:
        return None
    try:
        return get_cached_thread_state(manager, livekit_thread_id)
    except RuntimeError:
        logger.warning(
            "thread_list skipping unavailable LiveKit dispatcher fallback thread_id=%s",
            livekit_thread_id,
        )
        return None


def _ensure_livekit_dispatcher_thread(manager):
    thread = _get_cached_livekit_dispatcher_thread(manager)
    if thread is not None:
        return thread, None

    try:
        dispatcher_thread_id = async_to_sync(_warm_livekit_dispatcher_thread_for_api)()
    except (RuntimeError, TimeoutError) as exc:
        logger.warning("Unable to warm LiveKit dispatcher thread", exc_info=True)
        return None, str(exc)

    invalidate_thread_list_cache()
    try:
        return async_to_sync(manager.get_thread_state)(dispatcher_thread_id), None
    except RuntimeError as exc:
        logger.warning(
            "Unable to read warmed LiveKit dispatcher thread_id=%s",
            dispatcher_thread_id,
            exc_info=True,
        )
        return None, str(exc)


async def _warm_livekit_dispatcher_thread_for_api() -> str:
    return await asyncio.wait_for(
        warm_livekit_dispatcher_thread(
            timeout_seconds=min(15.0, DISPATCHER_THREAD_WARM_TIMEOUT_SECONDS),
        ),
        timeout=DISPATCHER_THREAD_WARM_TIMEOUT_SECONDS,
    )


def get_livekit_active_voice_thread_id() -> str | None:
    state = get_livekit_voice_route_state()
    return state.active_target_thread_id or state.dispatcher_thread_id


def _get_livekit_active_voice_thread(manager):
    livekit_thread_id = get_livekit_active_voice_thread_id()
    if not livekit_thread_id:
        return None
    try:
        return async_to_sync(manager.get_thread_state)(livekit_thread_id)
    except RuntimeError:
        logger.warning(
            "thread_active_voice skipping unavailable LiveKit thread_id=%s",
            livekit_thread_id,
        )
        return None


def _favorite_thread_list_response(
    request, manager, *, page: int, page_size: int, favorite: bool
):
    threads = _include_livekit_fallback_thread(manager, get_cached_thread_list(manager))
    filtered_threads = [
        thread
        for thread in threads
        if is_thread_favorite(thread.session_id) is favorite
    ]
    _refresh_projects_from_threads([thread.directory for thread in filtered_threads])
    count = len(filtered_threads)
    start = (page - 1) * page_size
    end = start + page_size
    page_threads = filtered_threads[start:end]
    next_url = (
        _offset_thread_page_url(request, page=page + 1, page_size=page_size)
        if end < count
        else None
    )
    previous_url = (
        _offset_thread_page_url(request, page=page - 1, page_size=page_size)
        if page > 1
        else None
    )
    return Response(
        {
            "count": count,
            "page": page,
            "page_size": page_size,
            "next": next_url,
            "previous": previous_url,
            "threads": [
                annotate_thread_payload(t.model_dump(mode="json")) for t in page_threads
            ],
        }
    )


@api_view(["GET"])
def thread_activity(request):
    """Report recently progressing runs for the cloud idle heartbeat.

    A status alone is not activity: waiting runs and running turns with no
    recent updates must eventually let a cloud workspace idle-stop.
    """
    manager = get_session_manager()
    threads = get_cached_thread_list(manager)
    activity_cutoff = datetime.now(UTC) - RUN_ACTIVITY_FRESHNESS
    active_run_count = sum(
        1
        for thread in threads
        if thread.status == ThreadStatus.running
        and _aware_datetime(thread.updated_at) >= activity_cutoff
    )
    return Response(
        {
            "active_run_count": active_run_count,
            "thread_count": len(threads),
        }
    )


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@api_view(["GET", "POST"])
def thread_list(request):
    """List all active threads or create a new one."""
    logger.info(
        "thread_list start method=%s path=%s auth=%s",
        request.method,
        request.path,
        _auth_debug_value(request),
    )
    manager = get_session_manager()

    if request.method == "POST":
        directory = request.data.get("directory")
        if not directory:
            return Response(
                {"error": "directory is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        thread = async_to_sync(manager.create_thread)(directory)
        invalidate_thread_list_cache()
        logger.info(
            "thread_list created thread_id=%s directory=%s",
            thread.session_id,
            thread.directory,
        )
        return Response(
            {
                "thread_id": thread.session_id,
                "directory": thread.directory,
            },
            status=status.HTTP_201_CREATED,
        )

    page, page_error = _parse_positive_int(
        request.query_params.get("page"),
        name="page",
        default=1,
    )
    page_size, page_size_error = _parse_positive_int(
        request.query_params.get("page_size"),
        name="page_size",
        default=DEFAULT_THREAD_PAGE_SIZE,
    )
    if page_error or page_size_error:
        return Response(
            {"error": page_error or page_size_error},
            status=status.HTTP_400_BAD_REQUEST,
        )
    favorite_filter, favorite_error = _parse_optional_bool(
        request.query_params.get("favorite"),
        name="favorite",
    )
    if favorite_error:
        return Response({"error": favorite_error}, status=status.HTTP_400_BAD_REQUEST)
    assert page is not None
    assert page_size is not None
    page_size = min(page_size, MAX_THREAD_PAGE_SIZE)
    if favorite_filter is not None:
        return _favorite_thread_list_response(
            request,
            manager,
            page=page,
            page_size=page_size,
            favorite=favorite_filter,
        )
    cursor = request.query_params.get("cursor") or None

    page_result = _get_thread_page_result(
        manager,
        page=page,
        page_size=page_size,
        cursor=cursor,
    )
    threads = page_result.threads
    _refresh_projects_from_threads([thread.directory for thread in threads])
    threads = _include_livekit_fallback_thread(manager, threads)
    count = (page - 1) * page_size + len(threads)
    if page_result.next_cursor:
        count += 1
    page_threads = threads
    next_url = (
        _thread_cursor_url(
            request,
            page=page + 1,
            page_size=page_size,
            cursor=page_result.next_cursor,
        )
        if page_result.next_cursor
        else None
    )
    previous_url = (
        _thread_page_url(request, page=page - 1, page_size=page_size)
        if page > 1
        else None
    )

    logger.info(
        "thread_list returning count=%s page=%s page_size=%s returned=%s",
        count,
        page,
        page_size,
        len(page_threads),
    )
    return Response(
        {
            "count": count,
            "page": page,
            "page_size": page_size,
            "next": next_url,
            "previous": previous_url,
            "threads": [
                annotate_thread_payload(t.model_dump(mode="json")) for t in page_threads
            ],
        }
    )


@api_view(["GET"])
def thread_dispatcher(request):
    """Return the LiveKit dispatcher thread without scanning the thread list."""
    manager = get_session_manager()
    thread, error = _ensure_livekit_dispatcher_thread(manager)
    if thread is None:
        return Response(
            {"error": error or "Unable to create LiveKit dispatcher thread"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response(annotate_thread_payload(thread.model_dump(mode="json")))


@api_view(["GET"])
def thread_active_voice(request):
    """Return the active voice conversation with turn text for the call page."""
    manager = get_session_manager()
    thread = _get_livekit_active_voice_thread(manager)
    if thread is None:
        thread, _error = _ensure_livekit_dispatcher_thread(manager)
    if thread is None:
        return Response(
            {"error": "Unable to create LiveKit dispatcher thread"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response(annotate_thread_payload(thread.model_dump(mode="json")))


@api_view(["GET", "DELETE"])
def thread_detail(request, thread_id):
    """Get or archive a thread."""
    manager = get_session_manager()

    if request.method == "DELETE":
        success = async_to_sync(manager.archive_thread)(thread_id)
        if not success:
            return Response(
                {"error": f"Thread {thread_id} not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        invalidate_thread_list_cache()
        return Response({"success": True})

    try:
        thread = async_to_sync(manager.get_thread_state)(thread_id)
    except RuntimeError as exc:
        if not is_thread_data_unavailable_error(exc):
            raise
        logger.info("thread_detail unreadable rollout thread_id=%s", thread_id)
        return Response(
            {
                "error": thread_error_message(exc),
                "code": THREAD_DATA_UNAVAILABLE_CODE,
            },
            status=status.HTTP_409_CONFLICT,
        )
    if thread is None:
        return Response(
            {"error": f"Thread {thread_id} not found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(
        annotate_thread_payload(thread.model_dump(mode="json"), thread_id=thread_id)
    )


@api_view(["GET", "PATCH"])
def thread_favorite(request, thread_id):
    """Read or update a thread's local favorite metadata."""
    if request.method == "GET":
        return Response(favorite_payload(thread_id))

    is_favorite = request.data.get("is_favorite")
    if not isinstance(is_favorite, bool):
        return Response(
            {"error": "is_favorite must be a boolean"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        payload = set_thread_favorite(thread_id, is_favorite)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    invalidate_thread_list_cache()
    return Response(payload)


@api_view(["GET", "PATCH"])
def thread_tags(request, thread_id):
    """Read or update a thread's local tag metadata."""
    if request.method == "GET":
        return Response(thread_tags_payload(thread_id))

    tags = request.data.get("tags")
    if not isinstance(tags, list):
        return Response(
            {"error": "tags must be a list"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        payload = set_thread_tags(thread_id, tags)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    invalidate_thread_list_cache()
    return Response(payload)


@api_view(["POST"])
def thread_interrupt(request, thread_id):
    """Interrupt the current turn in a thread.

    NOTE: The iOS app and React console prefer the WebSocket consumer for
    real-time thread interaction.
    """
    manager = get_session_manager()
    success = async_to_sync(manager.interrupt_turn)(thread_id)
    if not success:
        return Response(
            {"error": f"Thread {thread_id} not found or no active turn"},
            status=status.HTTP_404_NOT_FOUND,
        )
    invalidate_thread_list_cache()
    return Response({"success": True})


@api_view(["POST"])
def thread_start_turn(request, thread_id):
    """Start a new turn on a thread (non-blocking).

    NOTE: The iOS app and React console prefer the WebSocket consumer for
    real-time thread interaction.
    """
    manager = get_session_manager()
    prompt = request.data.get("prompt")
    if not prompt:
        return Response(
            {"error": "prompt is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        turn_id = async_to_sync(manager.start_turn)(thread_id, prompt)
    except (ValueError, RuntimeError) as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    invalidate_thread_list_cache()
    return Response(
        {"turn_id": turn_id, "status": "started"}, status=status.HTTP_201_CREATED
    )


@api_view(["POST"])
def thread_queue_turn(request, thread_id):
    """Queue a follow-up turn on a thread (non-blocking)."""
    manager = get_session_manager()
    prompt = request.data.get("prompt")
    if not prompt:
        return Response(
            {"error": "prompt is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        result = async_to_sync(manager.queue_turn)(thread_id, prompt)
    except (ValueError, RuntimeError) as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    invalidate_thread_list_cache()
    return Response(result, status=status.HTTP_202_ACCEPTED)


@api_view(["POST"])
def thread_steer_turn(request, thread_id):
    """Send text steering input to the active turn on a thread."""
    manager = get_session_manager()
    prompt = request.data.get("prompt")
    if not prompt:
        return Response(
            {"error": "prompt is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        result = async_to_sync(manager.steer_turn)(thread_id, prompt)
    except (ValueError, RuntimeError) as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    invalidate_thread_list_cache()
    return Response(result)
