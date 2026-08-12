"""Project report artifact API views."""

from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path

from asgiref.sync import async_to_sync
from django.http import FileResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.thread_sync.session_manager import get_session_manager
from openbase_coder_cli.openbase_coder_cli_app.item_tags import (
    report_tags_payload,
    set_report_tags,
)
from openbase_coder_cli.reports_service import (
    REPORTS_MAX_IMAGE_BYTES,
    REPORTS_MAX_TEXT_BYTES,
    _all_reports_items,
    _global_reports_projects,
    _list_reports_files,
    _reports_dir,
    _reports_file_payload,
    _reports_kind,
    _reports_summary,  # noqa: F401
    _resolve_report_origin,
    _resolve_reports_file,
    _resolve_reports_path,
    enrich_report_provenance,
    explicit_report_provenance,
)

REPORT_ACTION_PROMPT_MAX_CHARS = 24000
ACTION_HEADING_RE = re.compile(
    r"(?i)^#{1,6}\s*(action items?|next steps?|follow[- ]?ups?|todo|to do|implementation|recommendations?)\b"
)
MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+")
CHECKBOX_ACTION_RE = re.compile(r"(?im)^\s*(?:[-*]|\d+[.)])\s+\[\s\]\s+\S.*$")
ACTION_LINE_RE = re.compile(
    r"(?im)^\s*(?:[-*]|\d+[.)])\s+(?:\[[ xX]\]\s*)?"
    r"(?:action item|todo|to do|implement|fix|start|add|update|remove|investigate|follow up|follow-up)\b.*$"
)


def _extract_report_action_items(content: str) -> list[str]:
    action_items: list[str] = []
    seen: set[str] = set()

    def add(line: str) -> None:
        normalized = line.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            action_items.append(normalized)

    lines = content.splitlines()
    in_action_section = False
    for line in lines:
        if MARKDOWN_HEADING_RE.match(line):
            in_action_section = bool(ACTION_HEADING_RE.match(line))
            if in_action_section:
                add(line)
            continue
        if in_action_section:
            if line.strip():
                add(line)

    for pattern in (CHECKBOX_ACTION_RE, ACTION_LINE_RE):
        for match in pattern.finditer(content):
            add(match.group(0))

    return action_items[:80]


def _report_action_prompt(
    *,
    project_path: Path,
    relative_path: str,
    content: str,
    action_items: list[str],
) -> str:
    excerpt = content
    truncated = False
    if len(excerpt) > REPORT_ACTION_PROMPT_MAX_CHARS:
        excerpt = excerpt[:REPORT_ACTION_PROMPT_MAX_CHARS].rstrip()
        truncated = True

    action_text = "\n".join(action_items)
    truncation_note = (
        "\n\nThe report content below was truncated for prompt size."
        if truncated
        else ""
    )
    return (
        "Implement the action items from this report in the same project.\n\n"
        f"Project path: {project_path}\n"
        f"Report file: .reports/{relative_path}\n\n"
        "Focus on the report's actionable implementation work. Inspect the code first, "
        "keep the change scoped to the report, preserve existing behavior outside the "
        "requested work, and run focused verification when practical.\n\n"
        "Detected action items:\n"
        f"{action_text}\n\n"
        f"Report content:{truncation_note}\n\n"
        f"{excerpt}"
    )

@api_view(["GET"])
def project_reports(request):
    """List developer communication files for a project."""
    project_path = request.query_params.get("path", "").strip()
    if not project_path:
        return Response(
            {"error": "path is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    resolved = Path(project_path).expanduser().resolve()
    if not resolved.is_dir():
        return Response(
            {"error": f"Directory not found: {resolved}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    files = _list_reports_files(str(resolved))
    return Response(
        {
            "directory": str(_reports_dir(str(resolved))),
            "files": files,
        }
    )


@api_view(["GET"])
def global_reports_projects(request):
    """List global report source directories outside recent projects."""
    return Response({"projects": _global_reports_projects()})


@api_view(["GET"])
def all_project_reports(request):
    """List all report artifacts across recent and global report sources."""
    return Response({"items": _all_reports_items()})


@api_view(["POST"])
def project_reports_action(request):
    """Start an implementation turn for actionable report items."""
    project_path = str(request.data.get("path", "")).strip()
    relative_path = str(request.data.get("file", "")).strip()
    if not project_path:
        return Response(
            {"error": "path is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    resolved = Path(project_path).expanduser().resolve()
    if not resolved.is_dir():
        return Response(
            {"error": f"Directory not found: {resolved}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        file_path = _resolve_reports_file(str(resolved), relative_path)
    except FileNotFoundError:
        return Response(
            {"error": f"File not found: {relative_path}"},
            status=status.HTTP_404_NOT_FOUND,
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    kind = _reports_kind(file_path)
    if kind not in {"markdown", "text"}:
        return Response(
            {
                "error": "Only Markdown or text reports can start implementation turns.",
                "reason": "unsupported_report_kind",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    size = file_path.stat().st_size
    if size > REPORTS_MAX_TEXT_BYTES:
        return Response(
            {
                "error": "Report is too large to inspect for action items.",
                "reason": "report_too_large",
            },
            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    content = file_path.read_text(encoding="utf-8", errors="replace")
    action_items = _extract_report_action_items(content)
    if not action_items:
        return Response(
            {
                "error": "No action items were found in this report.",
                "reason": "no_action_items",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    origin, origin_error = _resolve_report_origin(
        content, resolved, file_path.stat().st_mtime
    )
    if origin is None:
        return Response(
            {
                "error": origin_error
                or "The originating Super Agent thread could not be determined.",
                "reason": "origin_unknown",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    prompt = _report_action_prompt(
        project_path=resolved,
        relative_path=relative_path,
        content=content,
        action_items=action_items,
    )
    try:
        turn_id = async_to_sync(get_session_manager().start_turn)(
            origin.thread_id, prompt
        )
    except ValueError as exc:
        return Response(
            {"error": str(exc), "reason": "turn_start_failed"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response(
        {
            "status": "started",
            "thread_id": origin.thread_id,
            "turn_id": turn_id,
            "thread_name": origin.label,
            "agent_name": origin.agent_name,
            "origin_source": origin.source,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET", "DELETE", "PATCH"])
def project_reports_file(request):
    """Return, update, or delete a renderable developer communication file."""
    project_path = request.query_params.get("path", "").strip()
    relative_path = request.query_params.get("file", "").strip()
    if not project_path:
        return Response(
            {"error": "path is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    resolved = Path(project_path).expanduser().resolve()
    if not resolved.is_dir():
        return Response(
            {"error": f"Directory not found: {resolved}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if request.method == "DELETE":
        try:
            file_path, reports_dir = _resolve_reports_path(str(resolved), relative_path)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if not file_path.exists():
            return Response(
                {"error": f"File not found: {relative_path}"},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not file_path.is_file():
            return Response(
                {"error": "Report path must be a file"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            file_payload = _reports_file_payload(file_path, reports_dir)
            file_path.unlink()
        except OSError as exc:
            return Response(
                {"error": f"Unable to delete report: {exc.strerror or exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({"deleted": True, "file": file_payload})

    if request.method == "PATCH":
        try:
            file_path, reports_dir = _resolve_reports_path(str(resolved), relative_path)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if not file_path.exists():
            return Response(
                {"error": f"File not found: {relative_path}"},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not file_path.is_file():
            return Response(
                {"error": "Report path must be a file"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        kind = _reports_kind(file_path)
        if kind not in {"markdown", "text"}:
            return Response(
                {"error": "Only markdown and text reports can be edited."},
                status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            )

        content = request.data.get("content")
        if not isinstance(content, str):
            return Response(
                {"error": "content must be a string"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(content.encode("utf-8")) > REPORTS_MAX_TEXT_BYTES:
            return Response(
                {"error": "File is too large to save as text"},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        try:
            file_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return Response(
                {"error": f"Unable to save report: {exc.strerror or exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "file": _reports_file_payload(file_path, reports_dir),
                "content": content,
            }
        )

    try:
        file_path = _resolve_reports_file(str(resolved), relative_path)
    except FileNotFoundError:
        return Response(
            {"error": f"File not found: {relative_path}"},
            status=status.HTTP_404_NOT_FOUND,
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    kind = _reports_kind(file_path)
    size = file_path.stat().st_size
    if kind in {"markdown", "text"}:
        if size > REPORTS_MAX_TEXT_BYTES:
            return Response(
                {"error": "File is too large to render as text"},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        content = file_path.read_text(encoding="utf-8", errors="replace")
        provenance = enrich_report_provenance(explicit_report_provenance(content))
        payload = {
            "file": _reports_file_payload(file_path, _reports_dir(str(resolved))),
            "content": content,
        }
        if provenance:
            payload["provenance"] = provenance.payload()
        return Response(
            payload
        )

    if kind == "image":
        if size > REPORTS_MAX_IMAGE_BYTES:
            return Response(
                {"error": "Image is too large to render inline"},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        media_type = (
            mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        )
        data = base64.b64encode(file_path.read_bytes()).decode("ascii")
        return Response(
            {
                "file": _reports_file_payload(file_path, _reports_dir(str(resolved))),
                "data_url": f"data:{media_type};base64,{data}",
            }
        )

    return Response(
        {
            "file": _reports_file_payload(file_path, _reports_dir(str(resolved))),
            "error": "This file type is not renderable in the console yet.",
        },
        status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    )


@api_view(["GET", "PATCH"])
def project_reports_tags(request):
    """Read or update local tag metadata for one report artifact."""
    project_path = request.query_params.get("path", "").strip()
    relative_path = request.query_params.get("file", "").strip()
    if not project_path:
        return Response(
            {"error": "path is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    resolved = Path(project_path).expanduser().resolve()
    if not resolved.is_dir():
        return Response(
            {"error": f"Directory not found: {resolved}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        _resolve_reports_file(str(resolved), relative_path)
    except FileNotFoundError:
        return Response(
            {"error": f"File not found: {relative_path}"},
            status=status.HTTP_404_NOT_FOUND,
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    if request.method == "GET":
        return Response(report_tags_payload(str(resolved), relative_path))

    tags = request.data.get("tags")
    if not isinstance(tags, list):
        return Response(
            {"error": "tags must be a list"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        payload = set_report_tags(str(resolved), relative_path, tags)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(payload)


@api_view(["GET"])
def project_reports_download(request):
    """Download any report artifact as a raw file."""
    project_path = request.query_params.get("path", "").strip()
    relative_path = request.query_params.get("file", "").strip()
    if not project_path:
        return Response(
            {"error": "path is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    resolved = Path(project_path).expanduser().resolve()
    if not resolved.is_dir():
        return Response(
            {"error": f"Directory not found: {resolved}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        file_path = _resolve_reports_file(str(resolved), relative_path)
    except FileNotFoundError:
        return Response(
            {"error": f"File not found: {relative_path}"},
            status=status.HTTP_404_NOT_FOUND,
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return FileResponse(
        file_path.open("rb"),
        as_attachment=True,
        filename=file_path.name,
        content_type=media_type,
    )
