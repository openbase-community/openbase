"""Shared local tag metadata for threads and reports."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from openbase_coder_cli.cli.utils import get_data_dir
from openbase_coder_cli.json_snapshot import JsonFileSnapshot

TAGS_FILE = "item-tags.json"
ItemKind = Literal["thread", "report"]

_lock = threading.Lock()


def tag_options_payload() -> dict[str, list[dict[str, Any]]]:
    payload = _read_tags()
    usage = _tag_usage_counts(payload)
    return {
        "tags": [
            {**option, "usage_count": usage.get(str(option["slug"]), 0)}
            for option in sorted(
                payload["options"].values(),
                key=lambda item: str(item["label"]).lower(),
            )
        ]
    }


def thread_tags_payload(thread_id: str | None) -> dict[str, Any]:
    normalized = _normalize_item_id(thread_id)
    return _item_tags_payload("thread", normalized)


def set_thread_tags(thread_id: str, tags: list[Any]) -> dict[str, Any]:
    normalized = _normalize_item_id(thread_id)
    if not normalized:
        raise ValueError("thread_id is required")
    return _set_item_tags("thread", normalized, tags)


def report_tags_payload(
    project_path: str | None, relative_path: str | None
) -> dict[str, Any]:
    project = _normalize_item_id(project_path)
    path = _normalize_report_path(relative_path)
    item_id = _report_item_id(project, path)
    payload = _item_tags_payload("report", item_id)
    payload.update({"project_path": project, "path": path})
    return payload


def set_report_tags(
    project_path: str,
    relative_path: str,
    tags: list[Any],
) -> dict[str, Any]:
    project = _normalize_item_id(project_path)
    path = _normalize_report_path(relative_path)
    if not project:
        raise ValueError("project_path is required")
    if not path:
        raise ValueError("file is required")
    payload = _set_item_tags("report", _report_item_id(project, path), tags)
    payload.update({"project_path": project, "path": path})
    return payload


def report_tags(project_path: str | None, relative_path: str | None) -> list[str]:
    return item_tag_labels(
        _read_tags(), "report", report_item_id(project_path, relative_path)
    )


def thread_tags(thread_id: str | None) -> list[str]:
    return item_tag_labels(_read_tags(), "thread", _normalize_item_id(thread_id))


def tags_snapshot() -> dict[str, Any]:
    """The current tags payload, for callers resolving many items at once.

    Read-only: pass it to ``item_tag_labels`` instead of calling
    ``report_tags``/``thread_tags`` per item.
    """
    return _read_tags()


def item_tag_labels(payload: dict[str, Any], kind: ItemKind, item_id: str) -> list[str]:
    """Just the labels — no tag-option catalog or usage counts.

    Report sweeps and thread-list annotation resolve tags once per item, so
    this must not pay the catalog-wide usage count the API payload carries.
    """
    if not item_id:
        return []
    entry = payload[f"{kind}s"].get(item_id)
    slugs = entry.get("tags") if isinstance(entry, dict) else []
    options = payload["options"]
    return [
        options[slug]["label"]
        for slug in slugs
        if isinstance(slug, str) and slug in options
    ]


def report_item_id(project_path: str | None, relative_path: str | None) -> str:
    return _report_item_id(
        _normalize_item_id(project_path), _normalize_report_path(relative_path)
    )


def _item_tags_payload(kind: ItemKind, item_id: str) -> dict[str, Any]:
    payload = _read_tags()
    assignments = payload[f"{kind}s"]
    entry = assignments.get(item_id) if item_id else None
    slugs = entry.get("tags") if isinstance(entry, dict) else []
    tags = [
        payload["options"][slug]["label"]
        for slug in slugs
        if isinstance(slug, str) and slug in payload["options"]
    ]
    return {
        f"{kind}_id": item_id,
        "tags": tags,
        "updated_at": entry.get("updated_at") if isinstance(entry, dict) else None,
        "tag_options": tag_options_payload()["tags"],
    }


def _set_item_tags(kind: ItemKind, item_id: str, raw_tags: list[Any]) -> dict[str, Any]:
    if not isinstance(raw_tags, list):
        raise ValueError("tags must be a list")
    with _lock:
        payload = _read_tags_unlocked()
        normalized_tags = _normalize_tag_inputs(raw_tags, payload["options"])
        now = _utc_now()
        for tag in normalized_tags:
            current = payload["options"].get(tag["slug"])
            payload["options"][tag["slug"]] = {
                "slug": tag["slug"],
                "label": current.get("label") if current else tag["label"],
                "created_at": current.get("created_at") if current else now,
                "updated_at": now,
            }

        assignments = payload[f"{kind}s"]
        if normalized_tags:
            assignments[item_id] = {
                f"{kind}_id": item_id,
                "tags": [tag["slug"] for tag in normalized_tags],
                "updated_at": now,
            }
        else:
            assignments.pop(item_id, None)
        _write_tags_unlocked(payload)
    return _item_tags_payload(kind, item_id)


def _normalize_tag_inputs(
    raw_tags: list[Any],
    options: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    tags: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_tag in raw_tags:
        label = raw_tag.strip() if isinstance(raw_tag, str) else ""
        if not label:
            continue
        slug = _slugify(label)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        existing = options.get(slug)
        tags.append(
            {
                "slug": slug,
                "label": str(existing.get("label")) if existing else label,
            }
        )
    return tags


def _read_tags() -> dict[str, Any]:
    """Read-only snapshot shared across callers; never mutate the result."""
    return _tags_snapshot.get(_tags_path())


def _read_tags_unlocked() -> dict[str, Any]:
    """Private, freshly parsed copy for write paths (caller holds ``_lock``)."""
    return _tags_snapshot.read_fresh(_tags_path())


def _parse_tags_payload(raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    return {
        "options": _read_options(raw_payload.get("options")),
        "threads": _read_assignments(raw_payload.get("threads"), "thread"),
        "reports": _read_assignments(raw_payload.get("reports"), "report"),
    }


def _read_options(raw_options: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_options, dict):
        return {}
    options: dict[str, dict[str, Any]] = {}
    for raw_slug, raw_option in raw_options.items():
        if not isinstance(raw_option, dict):
            continue
        slug = _slugify(raw_option.get("slug") or raw_slug)
        label = raw_option.get("label")
        if not slug or not isinstance(label, str) or not label.strip():
            continue
        options[slug] = {
            "slug": slug,
            "label": label.strip(),
            "created_at": _optional_string(raw_option.get("created_at")),
            "updated_at": _optional_string(raw_option.get("updated_at")),
        }
    return options


def _read_assignments(
    raw_assignments: Any, kind: ItemKind
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_assignments, dict):
        return {}
    assignments: dict[str, dict[str, Any]] = {}
    for raw_item_id, raw_entry in raw_assignments.items():
        item_id = _normalize_item_id(raw_item_id)
        if not item_id or not isinstance(raw_entry, dict):
            continue
        tags = [
            slug
            for raw_slug in raw_entry.get("tags", [])
            if (slug := _slugify(raw_slug))
        ]
        if not tags:
            continue
        assignments[item_id] = {
            f"{kind}_id": item_id,
            "tags": list(dict.fromkeys(tags)),
            "updated_at": _optional_string(raw_entry.get("updated_at")),
        }
    return assignments


def _write_tags_unlocked(payload: dict[str, Any]) -> None:
    path = _tags_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)
    _tags_snapshot.invalidate(path)


_tags_snapshot: JsonFileSnapshot[dict[str, Any]] = JsonFileSnapshot(_parse_tags_payload)


def _tag_usage_counts(payload: dict[str, Any]) -> dict[str, int]:
    usage: dict[str, int] = {}
    for assignments in (payload["threads"], payload["reports"]):
        for entry in assignments.values():
            for slug in entry.get("tags", []):
                usage[slug] = usage.get(slug, 0) + 1
    return usage


def _tags_path() -> Path:
    return get_data_dir() / TAGS_FILE


def _report_item_id(project_path: str, relative_path: str) -> str:
    return f"{project_path}\n{relative_path}" if project_path and relative_path else ""


def _normalize_report_path(value: Any) -> str:
    return str(value).strip().replace("\\", "/") if value is not None else ""


def _normalize_item_id(value: Any) -> str:
    return str(value).strip() if value is not None and str(value).strip() else ""


def _slugify(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
