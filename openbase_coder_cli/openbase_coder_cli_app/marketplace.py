"""Official marketplace API views: browse and install routines and skills.

The catalog is hosted anonymously on Openbase Cloud
(``/api/openbase/marketplace/...``). These views proxy that catalog to the
local console, add local install-state, and perform installs into Openbase
Coder local state (routines) and the agent-home skill roots (skills).

Skills are always co-installed for Claude Code and Codex together: a skill is
cloned into ``~/.agents/skills`` and symlinked into both Openbase agent homes.
MCP and CLI catalog entries are documentation-only and are not installable here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from asgiref.sync import async_to_sync
from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import dispatcher_config, skills_autolink
from openbase_coder_cli.mcp.session_manager import get_session_manager
from openbase_coder_cli.openbase_coder_cli_app.common import _clean_serializer_data
from openbase_coder_cli.openbase_coder_cli_app.routines import RoutineCreateSerializer
from openbase_coder_cli.openbase_coder_cli_app.skills import _home_skills_dir

CACHE_SECONDS = 300
_CATALOG_CACHE: dict[str, tuple[float, list[dict]]] = {}

CLOUD_FETCH_ERRORS = (HTTPError, URLError, TimeoutError, json.JSONDecodeError)


def _cloud_base_url() -> str:
    return str(
        getattr(settings, "WEB_BACKEND_URL", "https://app.openbase.cloud")
    ).rstrip("/")


def _catalog_url(kind: str) -> str:
    return f"{_cloud_base_url()}/api/openbase/marketplace/{kind}/"


def _read_url_json(url: str, *, timeout: int = 15) -> object:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _as_entries(payload: object) -> list[dict]:
    """Accept either a plain list or a DRF-paginated ``{results: [...]}``."""
    if isinstance(payload, dict):
        payload = payload.get("results", [])
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def _fetch_catalog(kind: str, *, force: bool = False) -> list[dict]:
    """Fetch and cache the full (unfiltered) cloud catalog for ``kind``."""
    now = time.monotonic()
    cached = _CATALOG_CACHE.get(kind)
    if not force and cached is not None and cached[0] > now:
        return cached[1]
    entries = _as_entries(_read_url_json(_catalog_url(kind)))
    _CATALOG_CACHE[kind] = (now + CACHE_SECONDS, entries)
    return entries


def _find_entry(kind: str, slug: str) -> dict | None:
    for entry in _fetch_catalog(kind):
        if entry.get("slug") == slug:
            return entry
    for entry in _fetch_catalog(kind, force=True):
        if entry.get("slug") == slug:
            return entry
    return None


def _matches_query(entry: dict, query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(
        str(entry.get(field, ""))
        for field in ("name", "tagline", "description", "slug")
    ).lower()
    return query in haystack


def _filter_entries(entries: list[dict], *, query: str, category: str) -> list[dict]:
    q = query.strip().lower()
    cat = category.strip().lower()
    result = []
    for entry in entries:
        if cat and str(entry.get("category", "")).lower() != cat:
            continue
        if not _matches_query(entry, q):
            continue
        result.append(entry)
    return result


def _categories(entries: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for entry in entries:
        name = str(entry.get("category") or "general")
        counts[name] = counts.get(name, 0) + 1
    return [{"name": name, "count": count} for name, count in sorted(counts.items())]


def _skill_installed(slug: str) -> bool:
    return (_home_skills_dir() / slug / "SKILL.md").is_file() or (
        (_home_skills_dir() / slug).is_dir()
        and bool(skills_autolink.list_skill_dirs(_home_skills_dir() / slug))
    )


def _bump_cloud_counter(kind: str, slug: str) -> None:
    """Best-effort popularity counter bump; never raises."""
    url = f"{_catalog_url(kind)}{slug}/install/"
    try:
        request = Request(url, data=b"", method="POST")
        with urlopen(request, timeout=5):
            return
    except Exception:  # noqa: BLE001 - best-effort, must not affect install
        return


# --- Skills -----------------------------------------------------------------


def _install_official_skill(entry: dict) -> dict:
    """Clone a kind=skill entry and co-install it into both agent homes.

    Raises ``RuntimeError`` on a fatal clone/layout failure.
    """
    slug = entry["slug"]
    repo_url = entry.get("repo_url") or ""
    clone_dir = _home_skills_dir() / slug

    if not clone_dir.exists():
        if not repo_url:
            raise RuntimeError(f"Skill '{slug}' has no repository URL.")
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(clone_dir)],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            if clone_dir.exists():
                shutil.rmtree(clone_dir, ignore_errors=True)
            raise RuntimeError(
                f"Failed to clone '{slug}': {completed.stderr.strip() or 'git error'}"
            )

    # A skill repo may itself be a skill dir (SKILL.md at root) or contain one
    # or more skill dirs. Collect the dir(s) to link into the agent homes.
    if (clone_dir / "SKILL.md").is_file():
        skill_dirs = [clone_dir]
    else:
        skill_dirs = skills_autolink.list_skill_dirs(clone_dir)
    if not skill_dirs:
        shutil.rmtree(clone_dir, ignore_errors=True)
        raise RuntimeError(f"Repository for '{slug}' contains no SKILL.md.")

    targets: dict[str, str] = {"home": "installed"}
    for scope, target_root in skills_autolink.auto_link_target_dirs().items():
        statuses = []
        for source_dir in skill_dirs:
            target_dir = target_root / source_dir.name
            try:
                statuses.append(
                    "created"
                    if skills_autolink.link_skill_dir(source_dir, target_dir)
                    else "existing"
                )
            except FileExistsError:
                statuses.append("conflict")
            except OSError:
                statuses.append("error")
        targets[scope] = (
            "conflict"
            if "conflict" in statuses
            else (
                "error"
                if "error" in statuses
                else ("created" if "created" in statuses else "existing")
            )
        )
    return {"slug": slug, "installed": True, "targets": targets}


@api_view(["GET"])
def marketplace_skills_catalog(request):
    query = request.query_params.get("q", "")
    category = request.query_params.get("category", "")
    try:
        entries = _fetch_catalog("skills")
    except CLOUD_FETCH_ERRORS as exc:
        return Response(
            {"error": f"Unable to load the skills catalog: {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    augmented = []
    for entry in entries:
        installed = entry.get("kind") == "skill" and _skill_installed(
            entry.get("slug", "")
        )
        augmented.append({**entry, "installed": installed})
    filtered = _filter_entries(augmented, query=query, category=category)
    return Response(
        {
            "source_url": _catalog_url("skills"),
            "categories": _categories(augmented),
            "entries": filtered,
        }
    )


@api_view(["POST"])
def marketplace_skills_install(request):
    slug = str(request.data.get("slug", "")).strip()
    if not slug:
        return Response(
            {"error": "slug is required"}, status=status.HTTP_400_BAD_REQUEST
        )
    try:
        entry = _find_entry("skills", slug)
    except CLOUD_FETCH_ERRORS as exc:
        return Response(
            {"error": f"Unable to load the skills catalog: {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    if entry is None:
        return Response(
            {"error": f"Skill '{slug}' was not found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    if entry.get("kind") != "skill":
        return Response(
            {
                "error": "This entry is documentation-only and cannot be installed automatically.",
                "docs_url": entry.get("docs_url", ""),
                "install_notes": entry.get("install_notes", ""),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        result = _install_official_skill(entry)
    except RuntimeError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    _bump_cloud_counter("skills", slug)
    return Response(result, status=status.HTTP_201_CREATED)


# --- Routines ---------------------------------------------------------------


def _expand_required_skills(
    required_slugs: list[str], skills_by_slug: dict[str, dict]
) -> list[dict]:
    expanded = []
    for slug in required_slugs:
        skill = skills_by_slug.get(slug, {})
        expanded.append(
            {
                "slug": slug,
                "name": skill.get("name", slug),
                "kind": skill.get("kind", "skill"),
                "installed": skill.get("kind") == "skill" and _skill_installed(slug),
                "docs_url": skill.get("docs_url", ""),
                "install_notes": skill.get("install_notes", ""),
            }
        )
    return expanded


def _schedule_summary(entry: dict) -> str:
    if entry.get("schedule_type") == "interval" and entry.get("interval_seconds"):
        minutes = max(1, int(entry["interval_seconds"]) // 60)
        return f"Every {minutes} min"
    return f"Daily at {entry.get('time') or '09:00'}"


@api_view(["GET"])
def marketplace_routines_catalog(request):
    query = request.query_params.get("q", "")
    category = request.query_params.get("category", "")
    try:
        entries = _fetch_catalog("routines")
        skills = _fetch_catalog("skills")
    except CLOUD_FETCH_ERRORS as exc:
        return Response(
            {"error": f"Unable to load the routines catalog: {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    skills_by_slug = {s.get("slug"): s for s in skills}
    manager = get_session_manager()
    installed_names = {
        str(r.get("name"))
        for r in (async_to_sync(manager.list_routines)() or {}).get("routines", [])
    }
    augmented = []
    for entry in entries:
        required = entry.get("required_skills") or []
        augmented.append(
            {
                **entry,
                "installed": entry.get("name") in installed_names,
                "schedule_summary": _schedule_summary(entry),
                "required_skills": _expand_required_skills(required, skills_by_slug),
            }
        )
    filtered = _filter_entries(augmented, query=query, category=category)
    return Response(
        {
            "source_url": _catalog_url("routines"),
            "categories": _categories(augmented),
            "entries": filtered,
        }
    )


def _unique_routine_name(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return f"{base}-{suffix}"


@api_view(["POST"])
def marketplace_routines_install(request):
    slug = str(request.data.get("slug", "")).strip()
    overrides = request.data.get("overrides") or {}
    if not slug:
        return Response(
            {"error": "slug is required"}, status=status.HTTP_400_BAD_REQUEST
        )
    try:
        entry = _find_entry("routines", slug)
        skills = _fetch_catalog("skills")
    except CLOUD_FETCH_ERRORS as exc:
        return Response(
            {"error": f"Unable to load the routines catalog: {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    if entry is None:
        return Response(
            {"error": f"Routine '{slug}' was not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    skills_by_slug = {s.get("slug"): s for s in skills}
    installed_skills: list[str] = []
    manual_dependencies: list[dict] = []
    for required_slug in entry.get("required_skills") or []:
        skill = skills_by_slug.get(required_slug)
        if not skill:
            continue
        if skill.get("kind") == "skill":
            if not _skill_installed(required_slug):
                try:
                    _install_official_skill(skill)
                    installed_skills.append(required_slug)
                except RuntimeError:
                    manual_dependencies.append(_dependency_payload(skill))
        else:
            manual_dependencies.append(_dependency_payload(skill))

    manager = get_session_manager()
    existing_names = {
        str(r.get("name"))
        for r in (async_to_sync(manager.list_routines)() or {}).get("routines", [])
    }

    override_name = str(overrides.get("name", "")).strip()
    if override_name:
        if override_name in existing_names:
            return Response(
                {"error": f"A routine named '{override_name}' already exists."},
                status=status.HTTP_409_CONFLICT,
            )
        name = override_name
    else:
        name = _unique_routine_name(entry.get("name") or slug, existing_names)

    payload: dict = {
        "name": name,
        "kind": entry.get("kind") or "agent",
        "scheduleType": entry.get("schedule_type") or "daily",
        "enabled": overrides.get("enabled", True),
    }
    if entry.get("prompt"):
        payload["prompt"] = entry["prompt"]
    if entry.get("command"):
        payload["command"] = entry["command"]
    if entry.get("command_timeout_seconds"):
        payload["commandTimeoutSeconds"] = entry["command_timeout_seconds"]
    payload["time"] = str(overrides.get("time") or entry.get("time") or "09:00")
    if entry.get("interval_seconds"):
        payload["intervalSeconds"] = int(
            overrides.get("intervalSeconds") or entry["interval_seconds"]
        )
    override_tz = str(overrides.get("timezone", "")).strip()
    if override_tz:
        payload["timezone"] = override_tz
    elif not entry.get("use_client_timezone", True) and entry.get("suggested_timezone"):
        payload["timezone"] = entry["suggested_timezone"]
    override_cwd = str(overrides.get("cwd", "")).strip()
    if override_cwd:
        payload["cwd"] = override_cwd

    serializer = RoutineCreateSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    cleaned = _clean_serializer_data(dict(serializer.validated_data))
    try:
        saved = async_to_sync(manager.save_routine)(cleaned)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    _bump_cloud_counter("routines", slug)
    return Response(
        {
            "routine": saved,
            "installed_name": name,
            "installed_skills": installed_skills,
            "manual_dependencies": manual_dependencies,
        },
        status=status.HTTP_201_CREATED,
    )


def _dependency_payload(skill: dict) -> dict:
    return {
        "slug": skill.get("slug"),
        "name": skill.get("name", skill.get("slug")),
        "kind": skill.get("kind", "skill"),
        "docs_url": skill.get("docs_url", ""),
        "install_notes": skill.get("install_notes", ""),
    }


# --- First-run recommended-skills prompt flag -------------------------------


@api_view(["GET", "PATCH"])
def marketplace_recommended_prompt(request):
    if request.method == "PATCH":
        dismissed_at = request.data.get("dismissedAt")
        dispatcher_config.set_marketplace_prompt_dismissed_at(
            str(dismissed_at) if dismissed_at else None
        )
    return Response(
        {"dismissedAt": dispatcher_config.marketplace_prompt_dismissed_at()}
    )
