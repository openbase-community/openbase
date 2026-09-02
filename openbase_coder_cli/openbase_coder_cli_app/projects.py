"""Project tracking and git diff API views."""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from pathlib import Path
from typing import Any

from multi.app_api import get_project_status, get_project_subrepos, get_repo_diff
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.openbase_coder_cli_app.common import _auth_debug_value
from openbase_coder_cli.openbase_coder_cli_app.reports import _reports_summary
from openbase_coder_cli.thread_sync.projects import (
    get_recent_projects as _get_recent_projects,
)
from openbase_coder_cli.thread_sync.projects import remove_project as _remove_project
from openbase_coder_cli.thread_sync.projects import track_project as _track_project

logger = logging.getLogger(__name__)

DEFAULT_PROJECT_PAGE_SIZE = 25
MAX_PROJECT_PAGE_SIZE = 100
PROJECT_LIST_CACHE_TTL_SECONDS = 15.0
PROJECT_METADATA_CACHE_TTL_SECONDS = 10.0
PROJECT_STATUS_WORKERS = 8
PROJECT_STATUS_TIMEOUT_SECONDS = 12.0
WORKTREE_SIBLING_SUFFIXES = ("worktrees", "work-trees")

_project_cache_lock = threading.Lock()
_project_executor = ThreadPoolExecutor(
    max_workers=PROJECT_STATUS_WORKERS,
    thread_name_prefix="project-status",
)
_cached_projects: list[dict[str, Any]] | None = None
_cached_projects_at = 0.0
_cached_project_metadata: dict[str, tuple[float, dict[str, Any]]] = {}
_refreshing_project_paths: set[str] = set()


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


def _project_page_url(request, *, page: int, page_size: int) -> str:
    query = request.query_params.copy()
    query["page"] = str(page)
    query["page_size"] = str(page_size)
    return f"{request.path}?{query.urlencode()}"


def _get_cached_recent_projects() -> list[dict[str, Any]]:
    global _cached_projects, _cached_projects_at

    now = time.monotonic()
    with _project_cache_lock:
        if (
            _cached_projects is not None
            and now - _cached_projects_at < PROJECT_LIST_CACHE_TTL_SECONDS
        ):
            return [dict(project) for project in _cached_projects]

    projects = _get_recent_projects()
    with _project_cache_lock:
        _cached_projects = [dict(project) for project in projects]
        _cached_projects_at = time.monotonic()
        return [dict(project) for project in _cached_projects]


def _invalidate_project_cache(path: str | None = None) -> None:
    global _cached_projects, _cached_projects_at

    with _project_cache_lock:
        _cached_projects = None
        _cached_projects_at = 0.0
        if path is None:
            _cached_project_metadata.clear()
        else:
            _cached_project_metadata.pop(path, None)


def _project_metadata(project_path: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "git_status": get_project_status(project_path),
        "stack": _project_stack(project_path),
    }
    metadata.update(_reports_summary(project_path))
    return metadata


def _cached_metadata(project_path: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _project_cache_lock:
        cached = _cached_project_metadata.get(project_path)
        if cached is None:
            return None
        cached_at, metadata = cached
        if now - cached_at >= PROJECT_METADATA_CACHE_TTL_SECONDS:
            return None
        return dict(metadata)


def _project_payload(project: dict[str, Any]) -> dict[str, Any]:
    payload = dict(project)
    metadata = _cached_metadata(str(project.get("path", "")))
    if metadata is None:
        payload.setdefault("git_status", "unknown")
        payload.setdefault("stack", None)
    else:
        payload.update(metadata)
    payload["worktrees"] = _worktree_children(str(project.get("path", "")))
    return payload


def _worktree_sibling_dirs(project_path: str) -> list[Path]:
    project_dir = Path(project_path).expanduser()
    parent = project_dir.parent
    if not parent.is_dir():
        return []
    project_name = project_dir.name
    try:
        siblings = list(parent.iterdir())
    except OSError:
        return []
    return [
        sibling
        for sibling in siblings
        if sibling.is_dir()
        and sibling.name != project_name
        and sibling.name.casefold().endswith(WORKTREE_SIBLING_SUFFIXES)
    ]


def _worktree_children(project_path: str) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sibling in _worktree_sibling_dirs(project_path):
        try:
            candidates = sorted(
                (item for item in sibling.iterdir() if item.is_dir()),
                key=lambda item: item.name.casefold(),
            )
        except OSError:
            continue
        for candidate in candidates:
            if not (candidate / "multi.json").is_file():
                continue
            path = str(candidate.resolve())
            key = path.casefold()
            if key in seen:
                continue
            seen.add(key)
            metadata = _cached_metadata(path)
            child = {"path": path, "source": "worktree"}
            if metadata is None:
                child.update({"git_status": "unknown", "stack": None})
            else:
                child.update(metadata)
            children.append(child)
    return children


def _refresh_project_metadata_task(project_path: str) -> None:
    try:
        metadata = _project_metadata(project_path)
        with _project_cache_lock:
            _cached_project_metadata[project_path] = (time.monotonic(), metadata)
    except Exception:
        logger.exception("project_metadata refresh failed path=%s", project_path)
    finally:
        with _project_cache_lock:
            _refreshing_project_paths.discard(project_path)


def _schedule_project_metadata_refresh(project_paths: list[str]) -> None:
    now = time.monotonic()
    scheduled: list[str] = []
    with _project_cache_lock:
        for project_path in project_paths:
            cached = _cached_project_metadata.get(project_path)
            if (
                cached is not None
                and now - cached[0] < PROJECT_METADATA_CACHE_TTL_SECONDS
            ):
                continue
            if project_path in _refreshing_project_paths:
                continue
            _refreshing_project_paths.add(project_path)
            scheduled.append(project_path)

    for project_path in scheduled:
        _project_executor.submit(_refresh_project_metadata_task, project_path)


def _refresh_project_metadata_now(project_paths: list[str]) -> list[dict[str, Any]]:
    unique_paths = list(dict.fromkeys(path for path in project_paths if path))
    futures = {
        _project_executor.submit(_project_metadata, project_path): project_path
        for project_path in unique_paths
    }
    refreshed: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + PROJECT_STATUS_TIMEOUT_SECONDS
    try:
        completed = as_completed(futures, timeout=PROJECT_STATUS_TIMEOUT_SECONDS)
        for future in completed:
            project_path = futures[future]
            remaining = max(0.0, deadline - time.monotonic())
            try:
                metadata = future.result(timeout=remaining)
            except Exception:
                logger.exception("project_metadata status failed path=%s", project_path)
                metadata = {"git_status": "unknown", "stack": None}
            refreshed[project_path] = metadata
            with _project_cache_lock:
                _cached_project_metadata[project_path] = (time.monotonic(), metadata)
    except TimeoutError:
        timed_out = [path for future, path in futures.items() if not future.done()]
        logger.warning("project_metadata status timed out paths=%s", timed_out)

    return [
        {"path": path, **refreshed.get(path, {"git_status": "unknown"})}
        for path in unique_paths
    ]


@api_view(["GET", "POST", "DELETE"])
def recent_projects(request):
    """List, add, or remove a tracked recent project path."""
    logger.info(
        "recent_projects start method=%s path=%s auth=%s",
        request.method,
        request.path,
        _auth_debug_value(request),
    )
    if request.method == "POST":
        path = request.data.get("path", "").strip()
        if not path:
            return Response(
                {"error": "path is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            return Response(
                {"error": f"Directory not found: {resolved}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        _track_project(str(resolved))
        _invalidate_project_cache(str(resolved))
        _schedule_project_metadata_refresh([str(resolved)])
        logger.info("recent_projects tracked path=%s", resolved)
        return Response({"path": str(resolved)}, status=status.HTTP_201_CREATED)

    if request.method == "DELETE":
        path = request.data.get("path", "").strip()
        if not path:
            return Response(
                {"error": "path is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        removed = _remove_project(path)
        _invalidate_project_cache(path)
        logger.info("recent_projects removed path=%s removed=%s", path, removed)
        return Response({"path": path, "removed": removed})

    page, page_error = _parse_positive_int(
        request.query_params.get("page"),
        name="page",
        default=1,
    )
    page_size, page_size_error = _parse_positive_int(
        request.query_params.get("page_size"),
        name="page_size",
        default=DEFAULT_PROJECT_PAGE_SIZE,
    )
    if page_error or page_size_error:
        return Response(
            {"error": page_error or page_size_error},
            status=status.HTTP_400_BAD_REQUEST,
        )
    assert page is not None
    assert page_size is not None
    page_size = min(page_size, MAX_PROJECT_PAGE_SIZE)

    projects = _get_cached_recent_projects()
    logger.info("recent_projects loaded tracked_count=%s", len(projects))
    start = (page - 1) * page_size
    end = start + page_size
    page_projects = projects[start:end]
    project_paths = [
        str(project.get("path", ""))
        for project in page_projects
        if str(project.get("path", ""))
    ]
    for project_path in list(project_paths):
        project_paths.extend(
            child["path"] for child in _worktree_children(project_path)
        )
    _schedule_project_metadata_refresh(project_paths)

    next_url = (
        _project_page_url(request, page=page + 1, page_size=page_size)
        if end < len(projects)
        else None
    )
    previous_url = (
        _project_page_url(request, page=page - 1, page_size=page_size)
        if page > 1
        else None
    )
    logger.info(
        "recent_projects returning count=%s page=%s page_size=%s returned=%s",
        len(projects),
        page,
        page_size,
        len(page_projects),
    )
    return Response(
        {
            "count": len(projects),
            "page": page,
            "page_size": page_size,
            "next": next_url,
            "previous": previous_url,
            "projects": [_project_payload(project) for project in page_projects],
        }
    )


@api_view(["GET"])
def project_status(request):
    """Return fresh metadata for visible projects."""
    paths = [path.strip() for path in request.query_params.getlist("path")]
    csv_paths = request.query_params.get("paths", "")
    if csv_paths:
        paths.extend(path.strip() for path in csv_paths.split(","))
    paths = [path for path in paths if path]
    if not paths:
        return Response({"projects": []})
    return Response({"projects": _refresh_project_metadata_now(paths)})


def _project_stack(project_path: str) -> str | None:
    metadata_path = Path(project_path) / ".openbase" / "project.json"
    if not metadata_path.is_file():
        return None
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    stack = data.get("stack")
    if isinstance(stack, str) and stack.strip():
        return stack.strip()
    return None


def _repo_diff(dir_path: str) -> dict:
    """Return a diff dict for a single git repo directory."""
    return {
        "path": dir_path,
        "name": Path(dir_path).name,
        "diff": get_repo_diff(dir_path),
    }


@api_view(["GET"])
def git_diff(request):
    """Return git diffs.

    Query params:
        path: optional project directory.  When given, returns diffs for that
              project and any sub-repos declared in its multi.json.  When
              omitted, returns diffs for all tracked recent projects.
    """
    single_path = request.query_params.get("path", "").strip()

    if single_path:
        resolved = str(Path(single_path).expanduser().resolve())
        if not Path(resolved).is_dir():
            return Response(
                {"error": f"Directory not found: {resolved}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        repositories = [_repo_diff(resolved)]
        for subrepo in get_project_subrepos(resolved):
            if Path(subrepo["path"]).is_dir():
                repositories.append(_repo_diff(subrepo["path"]))
        return Response({"repositories": repositories})

    # Default: all tracked projects
    projects = _get_recent_projects()
    repositories = []
    for project in projects:
        dir_path = project.get("path", "")
        if not dir_path or not Path(dir_path).is_dir():
            continue
        repositories.append(_repo_diff(dir_path))
    return Response({"repositories": repositories})
