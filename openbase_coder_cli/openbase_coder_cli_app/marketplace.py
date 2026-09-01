"""Read-only Cloud skill catalog and explicit pinned local installation."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.openbase_coder_cli_app.marketplace_install import (
    COMMIT_RE,
    SKILL_SLUG_RE,
    MarketplaceContractError,
    MarketplaceInstallError,
    _catalog_source,
    _download_skill_files,
    _install_skill_files,
    _installed_target_status,
    _target_conflicts,
    _verify_skill_files,
)
from openbase_coder_cli.openbase_coder_cli_app.skills import (
    GLOBAL_SKILL_SCOPES,
)
from openbase_coder_cli.services.onboarding import web_backend_url

CLOUD_SKILLS_PATH = "/api/openbase/marketplace/skills/"
CLOUD_ROUTINES_PATH = "/api/openbase/marketplace/routines/"
CATALOG_TIMEOUT_SECONDS = 15


@api_view(["GET"])
def marketplace_skills(request):
    query = request.query_params.get("q", "").strip()
    category = request.query_params.get("category", "").strip()
    try:
        raw_entries = _fetch_catalog(query=query, category=category)
        entries = [_catalog_entry_payload(entry) for entry in raw_entries]
    except (httpx.HTTPError, MarketplaceContractError, ValueError) as exc:
        return Response(
            {"error": f"Unable to load the Openbase skill catalog: {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    category_counts: dict[str, int] = {}
    for entry in entries:
        category_name = entry["category"]
        category_counts[category_name] = category_counts.get(category_name, 0) + 1
    return Response(
        {
            "categories": [
                {"name": name, "count": count}
                for name, count in sorted(category_counts.items())
            ],
            "entries": entries,
        }
    )


@api_view(["GET"])
def marketplace_routines(request):
    query = request.query_params.get("q", "").strip()
    category = request.query_params.get("category", "").strip()
    try:
        raw_entries = _fetch_routine_catalog(query=query, category=category)
        entries = [_routine_entry_payload(entry) for entry in raw_entries]
    except (httpx.HTTPError, MarketplaceContractError, ValueError) as exc:
        return Response(
            {"error": f"Unable to load the Openbase routine catalog: {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    category_counts: dict[str, int] = {}
    for entry in entries:
        category_name = entry["category"]
        category_counts[category_name] = category_counts.get(category_name, 0) + 1
    return Response(
        {
            "categories": [
                {"name": name, "count": count}
                for name, count in sorted(category_counts.items())
            ],
            "entries": entries,
            "read_only": True,
        }
    )


@api_view(["POST"])
def marketplace_skill_install(request):
    allowed_fields = {"slug", "commit", "targets", "confirmed"}
    unexpected_fields = sorted(set(request.data) - allowed_fields)
    if unexpected_fields:
        return Response(
            {
                "error": "Unexpected installation fields are not allowed.",
                "fields": unexpected_fields,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    slug = str(request.data.get("slug") or "").strip().lower()
    expected_commit = str(request.data.get("commit") or "").strip().lower()
    raw_targets = request.data.get("targets")
    if not SKILL_SLUG_RE.fullmatch(slug):
        return Response(
            {"error": "A valid catalog skill slug is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not COMMIT_RE.fullmatch(expected_commit):
        return Response(
            {"error": "The confirmed full catalog commit is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if request.data.get("confirmed") is not True:
        return Response(
            {"error": "Installation requires explicit confirmation."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not isinstance(raw_targets, list) or not raw_targets:
        return Response(
            {"error": "targets must be a non-empty list."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    targets = list(dict.fromkeys(str(target).strip() for target in raw_targets))
    if any(target not in GLOBAL_SKILL_SCOPES for target in targets):
        return Response(
            {"error": "A target skill scope is invalid."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        raw_entry = _fetch_catalog_skill(slug)
        entry = _catalog_entry_payload(raw_entry)
        if raw_entry.get("kind") != "skill":
            raise MarketplaceInstallError(
                "Only catalog skills can be installed through this endpoint."
            )
        source = _catalog_source(raw_entry.get("source"))
        if source is None:
            raise MarketplaceInstallError(
                "This catalog entry has no immutable source and cannot be installed."
            )
        if source.commit != expected_commit:
            raise MarketplaceInstallError(
                "The catalog commit changed after confirmation; review it again before installing."
            )
        conflicts = _target_conflicts(slug, source, targets)
        if conflicts:
            return Response(
                {
                    "error": "A skill with this slug already exists in a selected scope.",
                    "conflicts": conflicts,
                },
                status=status.HTTP_409_CONFLICT,
            )
        files = _download_skill_files(source)
        _verify_skill_files(files, source)
        results = _install_skill_files(
            slug=slug,
            source=source,
            entry=entry,
            files=files,
            targets=targets,
        )
    except httpx.HTTPStatusError as exc:
        response_status = (
            status.HTTP_404_NOT_FOUND
            if exc.response.status_code == 404
            else status.HTTP_502_BAD_GATEWAY
        )
        return Response(
            {"error": f"Unable to load catalog skill '{slug}'."},
            status=response_status,
        )
    except httpx.HTTPError as exc:
        return Response(
            {"error": f"Unable to download catalog skill '{slug}': {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    except (MarketplaceContractError, MarketplaceInstallError, ValueError) as exc:
        return Response(
            {"error": str(exc)},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except OSError as exc:
        return Response(
            {"error": f"Unable to install catalog skill '{slug}': {exc}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response(
        {
            "slug": slug,
            "commit": source.commit,
            "results": results,
        },
        status=(
            status.HTTP_200_OK
            if all(result["status"] == "already_installed" for result in results)
            else status.HTTP_201_CREATED
        ),
    )


def _fetch_catalog(*, query: str, category: str) -> list[dict[str, Any]]:
    return _fetch_catalog_list(CLOUD_SKILLS_PATH, query=query, category=category)


def _fetch_routine_catalog(*, query: str, category: str) -> list[dict[str, Any]]:
    return _fetch_catalog_list(CLOUD_ROUTINES_PATH, query=query, category=category)


def _fetch_catalog_list(
    path: str,
    *,
    query: str,
    category: str,
) -> list[dict[str, Any]]:
    parameters = {
        key: value
        for key, value in {"q": query, "category": category}.items()
        if value
    }
    url = f"{web_backend_url().rstrip('/')}{path}"
    if parameters:
        url = f"{url}?{urlencode(parameters)}"
    response = httpx.get(url, timeout=CATALOG_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise MarketplaceContractError("Catalog response must be a list.")
    return [entry for entry in payload if isinstance(entry, dict)]


def _fetch_catalog_skill(slug: str) -> dict[str, Any]:
    url = (
        f"{web_backend_url().rstrip('/')}{CLOUD_SKILLS_PATH}"
        f"{quote(slug, safe='')}/"
    )
    response = httpx.get(url, timeout=CATALOG_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise MarketplaceContractError("Catalog detail response must be an object.")
    if payload.get("slug") != slug:
        raise MarketplaceContractError("Catalog detail returned the wrong skill.")
    return payload


def _catalog_entry_payload(entry: dict[str, Any]) -> dict[str, Any]:
    slug = entry.get("slug")
    if not isinstance(slug, str) or not SKILL_SLUG_RE.fullmatch(slug):
        raise MarketplaceContractError("Catalog entry has an invalid slug.")
    kind = entry.get("kind")
    if kind not in {"skill", "mcp", "cli"}:
        raise MarketplaceContractError("Catalog entry has an invalid kind.")
    for field in ("name", "tagline", "description", "category"):
        if not isinstance(entry.get(field), str):
            raise MarketplaceContractError(
                f"Catalog entry has an invalid {field}."
            )
    docs_url = entry.get("docs_url")
    if not isinstance(docs_url, str):
        raise MarketplaceContractError("Catalog entry has an invalid docs_url.")
    if docs_url:
        parsed_docs_url = urlparse(docs_url)
        if (
            parsed_docs_url.scheme != "https"
            or not parsed_docs_url.hostname
            or parsed_docs_url.username
            or parsed_docs_url.password
            or parsed_docs_url.port is not None
        ):
            raise MarketplaceContractError(
                "Catalog documentation URL is not allowed."
            )
    source = _catalog_source(entry.get("source"))
    if source is not None and kind != "skill":
        raise MarketplaceContractError(
            "Only skill entries may declare an install source."
        )
    installed_targets = {
        scope: _installed_target_status(slug, source, scope)
        for scope in sorted(GLOBAL_SKILL_SCOPES)
    }
    return {
        key: entry.get(key)
        for key in (
            "id",
            "slug",
            "name",
            "tagline",
            "description",
            "category",
            "kind",
            "docs_url",
            "install_notes",
            "featured",
            "featured_rank",
            "install_count",
            "created_at",
            "updated_at",
        )
    } | {
        "source": (
            {
                "repository_url": source.repository_url,
                "commit": source.commit,
                "path": source.path,
                "integrity": source.integrity,
            }
            if source
            else None
        ),
        "installable": source is not None and entry.get("kind") == "skill",
        "installed_targets": installed_targets,
    }

def _routine_entry_payload(entry: dict[str, Any]) -> dict[str, Any]:
    slug = entry.get("slug")
    if not isinstance(slug, str) or not SKILL_SLUG_RE.fullmatch(slug):
        raise MarketplaceContractError("Routine entry has an invalid slug.")
    for field in ("name", "tagline", "description", "category"):
        if not isinstance(entry.get(field), str):
            raise MarketplaceContractError(
                f"Routine entry has an invalid {field}."
            )
    kind = entry.get("kind")
    if kind not in {"agent", "command"}:
        raise MarketplaceContractError("Routine entry has an invalid kind.")
    schedule_type = entry.get("schedule_type")
    if schedule_type not in {"daily", "interval"}:
        raise MarketplaceContractError(
            "Routine entry has an invalid schedule type."
        )
    required_skills = entry.get("required_skills")
    if not isinstance(required_skills, list) or any(
        not isinstance(item, str) or not SKILL_SLUG_RE.fullmatch(item)
        for item in required_skills
    ):
        raise MarketplaceContractError(
            "Routine entry has invalid required skills."
        )
    return {
        key: entry.get(key)
        for key in (
            "id",
            "slug",
            "name",
            "tagline",
            "description",
            "category",
            "kind",
            "prompt",
            "command",
            "command_timeout_seconds",
            "schedule_type",
            "time",
            "interval_seconds",
            "use_client_timezone",
            "suggested_timezone",
            "required_skills",
            "install_count",
            "created_at",
            "updated_at",
        )
    }
