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
from openbase_coder_cli.openbase_coder_cli_app.livekit_activity import (
    count_active_voice_calls,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_cache import (
    get_cached_thread_history_page,
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
from openbase_coder_cli.openbase_coder_cli_app.thread_models import (
    validate_model_for_thread,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_origins import (
    MANUAL_ORIGIN,
    set_thread_origin,
)
from openbase_coder_cli.services.fleet_aggregation import (
    FLEET_SCOPE_PARAM,
    FLEET_SCOPE_VALUE,
    ORIGIN_DEVICE_KEY,
    SourcePage,
    fleet_thread_detail,
    fleet_thread_page,
    thread_payload_sort_key,
)
from openbase_coder_cli.services.thread_push import moved_thread_detail
from openbase_coder_cli.thread_model_overrides import set_thread_model_override
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
MAX_THREAD_NAME_LENGTH = 200
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
    # A live voice call keeps a workspace busy even with no coding run.
    active_call_count = count_active_voice_calls()
    return Response(
        {
            "active_run_count": active_run_count,
            "active_call_count": active_call_count,
            "active": active_run_count > 0 or active_call_count > 0,
            "thread_count": len(threads),
        }
    )


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _resolve_new_thread_model(
    manager, requested: object, backend: str | None
) -> tuple[str, str | None]:
    """Validate a new thread's model and pick the backend that runs it.

    Returns ``(model, backend)``; ``backend`` is set only when the mixed
    backend facade must be pointed at a non-primary backend. Raises
    ValueError with a user-facing message for unknown models, models whose
    engine cannot run here, or a model that contradicts an explicit backend.
    """
    from openbase_coder_cli import dispatcher_config

    from .thread_models import thread_engine

    if not isinstance(requested, str) or not requested.strip():
        raise ValueError("model must be a non-empty string")
    model = requested.strip()
    location = dispatcher_config.backend_location()
    options = dispatcher_config.combined_model_options(location)
    selected = next(
        (option for option in options if option["id"].lower() == model.lower()), None
    )
    if selected is None or not selected["available"]:
        if selected and selected.get("unavailable_reason"):
            raise ValueError(selected["unavailable_reason"])
        known = ", ".join(option["id"] for option in options if option["available"])
        raise ValueError(
            f"Unknown or unavailable model {model!r}. Choose one of: {known}."
        )
    engine = dispatcher_config.model_engine(model)
    if backend:
        if thread_engine(backend) != engine:
            raise ValueError(f"model {model!r} does not run on backend {backend!r}")
        return model, None
    primary = getattr(manager, "_execution_backend", None)
    primary_runs_model = primary is None or thread_engine(primary) == engine
    manager_for_backend = getattr(manager, "manager_for_backend", None)
    if manager_for_backend is not None:
        identity = dispatcher_config.identity_for_model(model, location)
        if manager_for_backend(identity) is not None:
            return model, identity
    if not primary_runs_model:
        raise ValueError(f"model {model!r} is not available on this computer")
    return model, None


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
        backend = request.data.get("backend") or None
        create_kwargs = {}
        if backend:
            # Only the mixed-backend facade can target a backend; on a
            # single-backend install anything but that backend is an error.
            if getattr(manager, "manager_for_backend", None) is not None:
                if manager.manager_for_backend(backend) is None:
                    return Response(
                        {"error": f"backend {backend!r} is not configured"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                create_kwargs["backend"] = backend
            elif backend != getattr(manager, "_execution_backend", backend):
                return Response(
                    {"error": f"backend {backend!r} is not configured"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        requested_model = request.data.get("model")
        if requested_model is not None:
            try:
                model, model_backend = _resolve_new_thread_model(
                    manager, requested_model, backend
                )
            except ValueError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            if model_backend is not None:
                create_kwargs["backend"] = model_backend
        else:
            model = None
        thread = async_to_sync(manager.create_thread)(directory, **create_kwargs)
        if model:
            # The new-chat composer's model choice sticks to the thread like
            # the in-thread model dropdown does.
            set_thread_model_override(thread.session_id, model)
        # This endpoint is the only manual-entry chokepoint (console, desktop,
        # and mobile new-thread UIs all POST here); threads created any other
        # way (Super Agents MCP, dispatcher, voice) get no origin record and
        # are excluded from completion notifications. Stamping here, before
        # the response, guarantees the origin exists before the first turn
        # can start.
        set_thread_origin(thread.session_id, MANUAL_ORIGIN)
        invalidate_thread_list_cache()
        logger.info(
            "thread_list created thread_id=%s directory=%s backend=%s",
            thread.session_id,
            thread.directory,
            thread.backend,
        )
        return Response(
            {
                "thread_id": thread.session_id,
                "directory": thread.directory,
                "backend": thread.backend,
                "model": model,
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

    if request.query_params.get(FLEET_SCOPE_PARAM) == FLEET_SCOPE_VALUE:
        return _fleet_thread_list_response(
            request,
            manager,
            page=page,
            page_size=page_size,
            cursor=cursor,
        )

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


def _local_thread_source_page(manager, *, cursor: str | None, page_size: int):
    page_result = get_cached_thread_page(manager, limit=page_size, cursor=cursor)
    return SourcePage(
        items=[
            annotate_thread_payload(t.model_dump(mode="json"))
            for t in page_result.threads
        ],
        next_cursor=page_result.next_cursor,
    )


def _include_livekit_fallback_payload(manager, items: list[dict]) -> list[dict]:
    livekit_thread_id = get_livekit_shared_thread_id()
    if not livekit_thread_id or any(
        item.get("thread_id") == livekit_thread_id for item in items
    ):
        return items
    livekit_thread = _get_cached_livekit_dispatcher_thread(manager)
    if livekit_thread is None:
        return items
    payload = annotate_thread_payload(livekit_thread.model_dump(mode="json"))
    return sorted([*items, payload], key=thread_payload_sort_key, reverse=True)


def _fleet_thread_list_response(
    request, manager, *, page: int, page_size: int, cursor: str | None
):
    result = fleet_thread_page(
        page_size=page_size,
        cursor=cursor,
        fetch_local_page=lambda _page, local_cursor, size: _local_thread_source_page(
            manager, cursor=local_cursor, page_size=size
        ),
    )
    threads = result.threads
    if page == 1:
        threads = _include_livekit_fallback_payload(manager, threads)
    _refresh_projects_from_threads(
        [
            item["directory"]
            for item in threads
            if item.get("directory") and not item.get(ORIGIN_DEVICE_KEY)
        ]
    )
    count = (page - 1) * page_size + len(threads)
    if result.next_cursor:
        count += 1
    next_url = (
        _thread_cursor_url(
            request,
            page=page + 1,
            page_size=page_size,
            cursor=result.next_cursor,
        )
        if result.next_cursor
        else None
    )
    previous_url = (
        _thread_page_url(request, page=page - 1, page_size=page_size)
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
            "threads": threads,
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
        # Cache-and-coalesce: the detail view is polled every ~5s (and on each
        # socket event and window focus) on top of the live thread WebSocket,
        # and the dispatch page reads the same thread via /threads/dispatcher/
        # first — so an uncached read here means repeated, redundant app-server
        # round-trips for a thread another call just fetched. The 8s snapshot
        # staleness is absorbed by the client's reconcileThreadSnapshot, which
        # keeps live-streamed turns that post-date the snapshot.
        history_cursor = request.query_params.get("history_cursor")
        if history_cursor:
            # Older pages cache too: a not-loaded thread's page read can force
            # a full rollout-file parse, so re-reading a scrolled-back page
            # must not repeat that work within the TTL.
            thread = get_cached_thread_history_page(
                manager,
                thread_id,
                history_cursor,
            )
        else:
            thread = get_cached_thread_state(manager, thread_id)
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
    if (
        thread is not None
        and not history_cursor
        and request.query_params.get(FLEET_SCOPE_PARAM) == FLEET_SCOPE_VALUE
    ):
        # A thread pushed to another computer continues there: serve that
        # live copy (tagged with its origin) so the client follows it.
        moved_payload = moved_thread_detail(thread_id)
        if moved_payload is not None:
            return Response(moved_payload)
    if thread is None:
        # A fleet-scoped client may hold a thread that only exists on a peer
        # device (older than the sync window, or not yet exchanged) — serve
        # the peer's read-only copy rather than a 404.
        if request.query_params.get(FLEET_SCOPE_PARAM) == FLEET_SCOPE_VALUE:
            peer_payload = fleet_thread_detail(thread_id)
            if peer_payload is not None:
                return Response(peer_payload)
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


@api_view(["PATCH"])
def thread_name(request, thread_id):
    """Rename a thread on its coding backend."""
    name = request.data.get("name")
    if not isinstance(name, str) or not name.strip():
        return Response(
            {"error": "name must be a non-empty string"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if len(name) > MAX_THREAD_NAME_LENGTH:
        return Response(
            {"error": f"name must be at most {MAX_THREAD_NAME_LENGTH} characters"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    manager = get_session_manager()
    try:
        thread = async_to_sync(manager.rename_thread)(thread_id, name)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except RuntimeError as exc:
        return Response(
            {"error": thread_error_message(exc)},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    invalidate_thread_list_cache()
    if thread is None:
        return Response(
            {"error": f"Thread {thread_id} not found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(
        annotate_thread_payload(thread.model_dump(mode="json"), thread_id=thread_id)
    )


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


def _requested_turn_model(request, manager, thread_id) -> str | None:
    """Validate and persist an optional per-turn model switch.

    A `model` in a turn payload is the composer's model dropdown: it must stay
    on the thread's own backend, and it sticks — later turns without a model
    keep using it (stored as the thread's model override).

    Raises ValueError with a user-facing message on unknown or cross-backend
    models.
    """
    model = request.data.get("model")
    if not model or not isinstance(model, str):
        return None
    thread = async_to_sync(manager.get_thread_state)(thread_id)
    if thread is None:
        raise ValueError(f"Thread {thread_id} not found")
    model = validate_model_for_thread(thread.backend, model)
    set_thread_model_override(thread_id, model)
    return model


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
        model = _requested_turn_model(request, manager, thread_id)
        turn_id = async_to_sync(manager.start_turn)(thread_id, prompt, model=model)
    except (ValueError, RuntimeError) as e:
        # Surface the app-server's human-readable message (e.g. "thread not
        # loaded: <id>") rather than its raw JSON-RPC error envelope.
        return Response(
            {"error": thread_error_message(e)}, status=status.HTTP_400_BAD_REQUEST
        )
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
        model = _requested_turn_model(request, manager, thread_id)
        result = async_to_sync(manager.queue_turn)(thread_id, prompt, model=model)
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
