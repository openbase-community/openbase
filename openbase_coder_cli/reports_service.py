"""Django-free report discovery and query helpers."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from super_agents.app_server_client import DEFAULT_STATE_FILE
from super_agents.state import SessionRecord, read_state_file_locked

from openbase_coder_cli.thread_sync.projects import get_recent_projects as _get_recent_projects
from openbase_coder_cli.paths import CODEX_HOME_DIR, NORMAL_CODEX_HOME_DIR

REPORTS_DIRECTORY = ".reports"
REPORTS_TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}
REPORTS_IMAGE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
REPORTS_MAX_FILES = 200
REPORTS_MAX_TEXT_BYTES = 1024 * 1024
REPORTS_MAX_IMAGE_BYTES = 5 * 1024 * 1024
REPORTS_TITLE_SCAN_BYTES = 64 * 1024
HOME_REPORTS_PROJECT_DIR = Path.home()
REPORT_ORIGIN_TIME_WINDOW_SECONDS = 10 * 60
SUPER_AGENTS_STATE_FILE_ENV = "SUPER_AGENTS_STATE_FILE"
REPORT_THREAD_ID_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?(?:super agent\s+)?thread\s+id(?:\*\*)?\s*:\s*`?([A-Za-z0-9._:-]+)`?\s*$"
)
REPORT_THREAD_NAME_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?super agent thread name(?:\*\*)?\s*:\s*(.+?)\s*$"
)
REPORT_AGENT_NAME_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?(?:super agent\s+)?agent\s+name(?:\*\*)?\s*:\s*(.+?)\s*$"
)
REPORT_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
REPORT_HTML_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
REPORT_PROVENANCE_JSON_RE = re.compile(
    r"(?is)\b(?:openbase[-_\s]?report|report[-_\s]?provenance|openbase[-_\s]?provenance)\s*:\s*(\{.*?\})\s*$"
)
REPORT_FILENAME_DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)(?!\d)"
)
REPORT_MARKDOWN_TITLE_RE = re.compile(r"^(?: {0,3})#(?:\s+|$)(.*?)\s*#*\s*$")


@dataclass(frozen=True)
class ReportActionOrigin:
    thread_id: str
    label: str | None = None
    agent_name: str | None = None
    source: str = "unknown"


@dataclass(frozen=True)
class ReportProvenance:
    thread_id: str | None = None
    thread_name: str | None = None
    agent_name: str | None = None
    source: str = "unknown"

    def payload(self) -> dict[str, str]:
        payload = {"source": self.source}
        if self.thread_id:
            payload["thread_id"] = self.thread_id
        if self.thread_name:
            payload["thread_name"] = self.thread_name
        if self.agent_name:
            payload["agent_name"] = self.agent_name
        return payload


@dataclass(frozen=True)
class ReportQuery:
    project: str | None = None
    repo: str | None = None
    tag: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    date_basis: str = "modified_time"


def _reports_dir(project_path: str) -> Path:
    return Path(project_path).expanduser().resolve() / REPORTS_DIRECTORY


def _reports_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in REPORTS_TEXT_EXTENSIONS:
        return "markdown" if suffix in {".md", ".markdown"} else "text"
    if suffix in REPORTS_IMAGE_EXTENSIONS:
        return "image"
    return "other"


def _report_filename_date(relative_path: str) -> str | None:
    match = REPORT_FILENAME_DATE_RE.search(relative_path)
    if not match:
        return None
    try:
        return date(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
        ).isoformat()
    except ValueError:
        return None


def _report_markdown_title(path: Path) -> str | None:
    fenced = False
    try:
        content = path.read_bytes()[:REPORTS_TITLE_SCAN_BYTES].decode(
            "utf-8",
            errors="replace",
        )
    except OSError:
        return None

    for line in content.splitlines():
        trimmed_start = line.lstrip()
        if trimmed_start.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue

        match = REPORT_MARKDOWN_TITLE_RE.match(line)
        title = match.group(1).strip() if match else ""
        if title:
            return title
    return None


def _reports_file_payload(path: Path, reports_dir: Path) -> dict[str, Any]:
    from openbase_coder_cli.openbase_coder_cli_app.item_tags import report_tags

    stat = path.stat()
    relative_path = str(path.relative_to(reports_dir))
    project_path = str(reports_dir.parent)
    kind = _reports_kind(path)
    return {
        "path": relative_path,
        "name": path.name,
        "kind": kind,
        "title": _report_markdown_title(path) if kind == "markdown" else None,
        "size": stat.st_size,
        "updated_at": stat.st_mtime,
        "filename_date": _report_filename_date(relative_path),
        "date_basis": "modified_time",
        "tags": report_tags(project_path, relative_path),
    }


def _list_reports_files(project_path: str) -> list[dict[str, Any]]:
    reports_dir = _reports_dir(project_path).resolve()
    if not reports_dir.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for candidate in sorted(reports_dir.rglob("*")):
        if len(files) >= REPORTS_MAX_FILES:
            break
        if not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve()
            resolved.relative_to(reports_dir)
            files.append(_reports_file_payload(resolved, reports_dir))
        except (OSError, ValueError):
            continue
    return sorted(files, key=lambda item: item["updated_at"], reverse=True)


def _reports_summary(project_path: str) -> dict[str, Any]:
    files = _list_reports_files(project_path)
    updated_at = files[0]["updated_at"] if files else None
    return {"reports_count": len(files), "reports_updated_at": updated_at}


def _global_reports_projects() -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for project_dir in (CODEX_HOME_DIR, NORMAL_CODEX_HOME_DIR, HOME_REPORTS_PROJECT_DIR):
        try:
            resolved = project_dir.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not _reports_dir(str(resolved)).is_dir():
            continue
        seen.add(resolved)
        project: dict[str, Any] = {"path": str(resolved), "global_reports": True}
        project.update(_reports_summary(str(resolved)))
        projects.append(project)
    return projects


def _all_reports_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for project in _global_reports_projects() + _get_recent_projects():
        project_path = str(project.get("path", "")).strip()
        if not project_path:
            continue
        try:
            resolved = Path(project_path).expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        project_payload = dict(project)
        project_payload["path"] = str(resolved)
        for file_payload in _list_reports_files(str(resolved)):
            items.append(
                {
                    "id": f"{resolved}:{file_payload['path']}",
                    "project": project_payload,
                    "file": file_payload,
                    "updated_at": file_payload["updated_at"],
                }
            )
    return sorted(items, key=lambda item: item["updated_at"], reverse=True)


def list_report_items(query: ReportQuery | None = None) -> list[dict[str, Any]]:
    items = _all_reports_items()
    return filter_report_items(items, query) if query else items


def filter_report_items(
    items: list[dict[str, Any]],
    query: ReportQuery | None,
) -> list[dict[str, Any]]:
    if query is None:
        return items
    if query.date_basis != "modified_time":
        raise ValueError("Only modified_time date filtering is currently supported.")
    filtered = items
    if query.project:
        needle = query.project.casefold()
        filtered = [
            item
            for item in filtered
            if needle in str(item.get("project", {}).get("path", "")).casefold()
            or needle in str(item.get("project", {}).get("name", "")).casefold()
        ]
    if query.repo:
        needle = query.repo.casefold()
        filtered = [
            item
            for item in filtered
            if Path(str(item.get("project", {}).get("path", ""))).name.casefold()
            == needle
        ]
    if query.tag:
        needle = query.tag.casefold()
        filtered = [
            item
            for item in filtered
            if any(
                needle == str(tag).casefold()
                for tag in item.get("file", {}).get("tags", [])
            )
        ]
    if query.date_from or query.date_to:
        filtered = [
            item
            for item in filtered
            if _item_modified_date_matches(item, query.date_from, query.date_to)
        ]
    return filtered


def _item_modified_date_matches(
    item: dict[str, Any],
    date_from: date | None,
    date_to: date | None,
) -> bool:
    updated_at = item.get("updated_at")
    if not isinstance(updated_at, int | float):
        return False
    item_date = datetime.fromtimestamp(updated_at, tz=UTC).astimezone().date()
    if date_from and item_date < date_from:
        return False
    if date_to and item_date > date_to:
        return False
    return True


def _resolve_reports_path(project_path: str, relative_path: str) -> tuple[Path, Path]:
    if not relative_path:
        raise ValueError("file is required")
    reports_dir = _reports_dir(project_path).resolve()
    candidate = (reports_dir / relative_path).resolve()
    try:
        candidate.relative_to(reports_dir)
    except ValueError as exc:
        raise ValueError("file must be inside .reports") from exc
    return candidate, reports_dir


def _resolve_reports_file(project_path: str, relative_path: str) -> Path:
    candidate, _reports_dir_path = _resolve_reports_path(project_path, relative_path)
    if not candidate.is_file():
        raise FileNotFoundError(relative_path)
    return candidate


def resolve_report_item(identifier: str) -> dict[str, Any]:
    normalized = identifier.strip()
    if not normalized:
        raise ValueError("report id or path is required")
    matches = [
        item
        for item in _all_reports_items()
        if normalized
        in {
            str(item.get("id", "")),
            str(item.get("file", {}).get("path", "")),
            str(
                Path(str(item.get("project", {}).get("path", "")))
                / REPORTS_DIRECTORY
                / str(item.get("file", {}).get("path", ""))
            ),
        }
    ]
    if not matches:
        raise FileNotFoundError(normalized)
    if len(matches) > 1:
        raise ValueError(
            "Report identifier is ambiguous; use the full report id or absolute path."
        )
    return matches[0]


def read_report_item(identifier: str) -> dict[str, Any]:
    item = resolve_report_item(identifier)
    project_path = str(item["project"]["path"])
    relative_path = str(item["file"]["path"])
    file_path = _resolve_reports_file(project_path, relative_path)
    kind = _reports_kind(file_path)
    payload: dict[str, Any] = {"item": item, "file": item["file"]}
    if kind in {"markdown", "text"}:
        size = file_path.stat().st_size
        if size > REPORTS_MAX_TEXT_BYTES:
            raise ValueError("Report is too large to read as text.")
        payload["content"] = file_path.read_text(encoding="utf-8", errors="replace")
    else:
        payload["error"] = "Only markdown and text reports can be read as text."
    return payload


def parse_report_date_range(
    value: str | None,
    *,
    now: datetime | None = None,
) -> tuple[date | None, date | None]:
    if not value:
        return None, None
    today = (now or datetime.now().astimezone()).date()
    normalized = value.strip().lower()
    if normalized == "today":
        return today, today
    if normalized == "yesterday":
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    if normalized.endswith("d") and normalized[:-1].isdigit():
        days = int(normalized[:-1])
        if days < 1:
            raise ValueError("Relative day ranges must be at least 1d.")
        return today - timedelta(days=days - 1), today
    if ".." in normalized:
        start_raw, end_raw = normalized.split("..", 1)
        return (
            _parse_report_date(start_raw) if start_raw else None,
            _parse_report_date(end_raw) if end_raw else None,
        )
    parsed = _parse_report_date(normalized)
    return parsed, parsed


def _parse_report_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "Use today, yesterday, Nd, YYYY-MM-DD, or YYYY-MM-DD..YYYY-MM-DD."
        ) from exc


def _super_agents_state_path() -> Path:
    configured = os.environ.get(SUPER_AGENTS_STATE_FILE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_STATE_FILE


def _parse_report_thread_id(content: str) -> str | None:
    match = REPORT_THREAD_ID_RE.search(content)
    if not match:
        return None
    return match.group(1).strip() or None


def _parse_report_thread_name(content: str) -> str | None:
    match = REPORT_THREAD_NAME_RE.search(content)
    if not match:
        return None
    return match.group(1).strip() or None


def _parse_report_agent_name(content: str) -> str | None:
    match = REPORT_AGENT_NAME_RE.search(content)
    if not match:
        return None
    return match.group(1).strip() or None


def _normalize_provenance_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _provenance_from_mapping(
    values: dict[str, Any],
    *,
    source: str,
) -> ReportProvenance | None:
    normalized = {_normalize_provenance_key(str(key)): value for key, value in values.items()}
    thread_id = _optional_metadata_string(
        normalized.get("thread_id")
        or normalized.get("threadid")
        or normalized.get("super_agent_thread_id")
        or normalized.get("super_agents_thread_id")
    )
    thread_name = _optional_metadata_string(
        normalized.get("thread_name")
        or normalized.get("thread")
        or normalized.get("name")
        or normalized.get("super_agent_thread_name")
        or normalized.get("super_agents_thread_name")
    )
    agent_name = _optional_metadata_string(
        normalized.get("agent_name")
        or normalized.get("agent")
        or normalized.get("super_agent_agent_name")
    )
    if not thread_id and not thread_name and not agent_name:
        return None
    return ReportProvenance(
        thread_id=thread_id,
        thread_name=thread_name,
        agent_name=agent_name,
        source=source,
    )


def _optional_metadata_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().strip("\"'")
    return normalized or None


def _parse_simple_front_matter(content: str) -> dict[str, Any]:
    match = REPORT_FRONT_MATTER_RE.match(content)
    if not match:
        return {}
    values: dict[str, Any] = {}
    prefix_stack: list[str] = []
    for raw_line in match.group(1).splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        value = raw_value.strip()
        normalized_key = _normalize_provenance_key(key)
        if indent == 0:
            prefix_stack = [normalized_key] if not value else []
        if value:
            keys = [normalized_key]
            if prefix_stack and indent > 0:
                keys.append(f"{prefix_stack[-1]}_{normalized_key}")
            for candidate_key in keys:
                values[candidate_key] = value
    return values


def _parse_hidden_comment_provenance(content: str) -> ReportProvenance | None:
    for match in REPORT_HTML_COMMENT_RE.finditer(content):
        comment = match.group(1).strip()
        json_match = REPORT_PROVENANCE_JSON_RE.search(comment)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                provenance = _provenance_from_mapping(data, source="html_comment")
                if provenance:
                    return provenance

        values: dict[str, Any] = {}
        for line in comment.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            values[key] = value.strip()
        provenance = _provenance_from_mapping(values, source="html_comment")
        if provenance:
            return provenance
    return None


def explicit_report_provenance(content: str) -> ReportProvenance | None:
    front_matter = _parse_simple_front_matter(content)
    provenance = _provenance_from_mapping(front_matter, source="front_matter")
    if provenance:
        return provenance

    provenance = _parse_hidden_comment_provenance(content)
    if provenance:
        return provenance

    thread_id = _parse_report_thread_id(content)
    thread_name = _parse_report_thread_name(content)
    agent_name = _parse_report_agent_name(content)
    if thread_id or thread_name or agent_name:
        return ReportProvenance(
            thread_id=thread_id,
            thread_name=thread_name,
            agent_name=agent_name,
            source="embedded_text",
        )
    return None


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _session_sort_time(session: SessionRecord) -> datetime:
    return (
        _parse_iso_timestamp(session.last_finished_at)
        or _parse_iso_timestamp(session.last_started_at)
        or _parse_iso_timestamp(session.updated_at)
        or datetime.fromtimestamp(0, tz=UTC)
    )


def _session_report_delta_seconds(
    session: SessionRecord,
    report_updated_at: float,
) -> float:
    report_time = datetime.fromtimestamp(report_updated_at, tz=UTC)
    values = [
        session.last_finished_at,
        session.last_started_at,
        session.last_event_at,
        session.updated_at,
    ]
    if session.turns:
        for turn in session.turns.values():
            values.extend([turn.finished_at, turn.updated_at, turn.started_at])
    deltas = [
        abs((report_time - parsed).total_seconds())
        for parsed in (_parse_iso_timestamp(value) for value in values)
        if parsed is not None
    ]
    return min(deltas) if deltas else float("inf")


def _session_matches_cwd(session: SessionRecord, project_path: Path) -> bool:
    if not session.cwd:
        return False
    try:
        return Path(session.cwd).expanduser().resolve() == project_path
    except OSError:
        return False


def _origin_from_session(session: SessionRecord, source: str) -> ReportActionOrigin:
    return ReportActionOrigin(
        thread_id=session.thread_id,
        label=session.label,
        agent_name=session.agent_name,
        source=source,
    )


def enrich_report_provenance(provenance: ReportProvenance | None) -> ReportProvenance | None:
    if provenance is None:
        return None
    if not provenance.thread_id:
        return provenance
    state = read_state_file_locked(_super_agents_state_path())
    session = state.sessions.get(provenance.thread_id)
    if session is None:
        return provenance
    return ReportProvenance(
        thread_id=provenance.thread_id,
        thread_name=provenance.thread_name or session.label,
        agent_name=provenance.agent_name or session.agent_name,
        source=provenance.source,
    )


def _resolve_report_origin(
    content: str,
    project_path: Path,
    report_updated_at: float,
) -> tuple[ReportActionOrigin | None, str | None]:
    state = read_state_file_locked(_super_agents_state_path())
    explicit_thread_id = _parse_report_thread_id(content)
    if explicit_thread_id:
        session = state.sessions.get(explicit_thread_id)
        if session is not None:
            return _origin_from_session(session, "report_thread_id"), None
        return ReportActionOrigin(thread_id=explicit_thread_id, source="report_thread_id"), None

    explicit_name = _parse_report_thread_name(content)
    if explicit_name:
        label_matches = [
            session for session in state.sessions.values() if session.label == explicit_name
        ]
        cwd_matches = [
            session
            for session in label_matches
            if _session_matches_cwd(session, project_path)
        ]
        matches = cwd_matches or label_matches
        if matches:
            selected = sorted(matches, key=_session_sort_time, reverse=True)[0]
            return _origin_from_session(selected, "report_thread_name"), None
        return None, f"No Super Agent thread named {explicit_name!r} was found."

    cwd_matches = [
        session
        for session in state.sessions.values()
        if _session_matches_cwd(session, project_path)
    ]
    if len(cwd_matches) == 1:
        return _origin_from_session(cwd_matches[0], "project_thread"), None
    if len(cwd_matches) > 1:
        scored = sorted(
            (
                (_session_report_delta_seconds(session, report_updated_at), session)
                for session in cwd_matches
            ),
            key=lambda item: item[0],
        )
        close_matches = [
            item for item in scored if item[0] <= REPORT_ORIGIN_TIME_WINDOW_SECONDS
        ]
        if len(close_matches) == 1:
            return _origin_from_session(
                close_matches[0][1],
                "project_thread_report_time",
            ), None
        return (
            None,
            "Multiple Super Agent threads match this project, and the report does not identify which one created it.",
        )
    return None, "The originating Super Agent thread could not be determined from this report."
