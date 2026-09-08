"""Skill file management API views."""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import dispatcher_config, skills_autolink
from openbase_coder_cli.paths import (
    CLAUDE_CONFIG_DIR,
    CODEX_HOME_DIR,
)

PRINTING_PRESS_REGISTRY_URL = "https://raw.githubusercontent.com/mvanhorn/printing-press-library/main/registry.json"
PRINTING_PRESS_SKILL_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/mvanhorn/printing-press-library/main/"
    "cli-skills/pp-{name}/SKILL.md"
)
PRINTING_PRESS_TARGET_SCOPES = {"home", "codex", "claude"}
GLOBAL_SKILL_SCOPES = {
    "home",
    "codex",
    "claude",
}
PRINTING_PRESS_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PRINTING_PRESS_REGISTRY_CACHE_SECONDS = 300
_PRINTING_PRESS_REGISTRY_CACHE: dict | None = None
_PRINTING_PRESS_REGISTRY_CACHE_EXPIRES_AT = 0.0


def _home_skills_dir() -> Path:
    return Path.home() / ".agents" / "skills"


def _codex_skills_dir() -> Path:
    return CODEX_HOME_DIR / "skills"


def _claude_skills_dir() -> Path:
    return CLAUDE_CONFIG_DIR / "skills"


def _skills_dir(project_path: str | None, scope: str = "home") -> Path:
    """Return the skills directory for a project or global scope."""
    if project_path:
        return Path(project_path).expanduser().resolve() / ".agents" / "skills"
    if scope == "codex":
        return _codex_skills_dir()
    if scope == "claude":
        return _claude_skills_dir()
    if scope == "home":
        return _home_skills_dir()
    raise ValueError("invalid skill scope")


def _skill_scope_payload() -> list[dict[str, str]]:
    return [
        {
            "key": "home",
            "label": "Personal Codex skills",
            "skills_dir": str(_home_skills_dir()),
        },
        {
            "key": "codex",
            "label": "Codex skills",
            "skills_dir": str(_codex_skills_dir()),
        },
        {
            "key": "claude",
            "label": "Claude Code skills",
            "skills_dir": str(_claude_skills_dir()),
        },
    ]


def _list_skill_entries(skills_root: Path) -> list[dict[str, str]]:
    skills = []
    if skills_root.is_dir():
        for child in sorted(skills_root.iterdir()):
            skill_file = child / "SKILL.md"
            if child.is_dir() and skill_file.is_file():
                entry = {
                    "name": child.name,
                    "path": str(skill_file),
                    "dir_path": str(child),
                }
                if child.is_symlink():
                    entry["source_dir_path"] = str(child.resolve())
                if child.is_symlink() or skill_file.is_symlink():
                    entry["source_path"] = str(skill_file.resolve())
                skills.append(entry)
    return skills


def _skill_file(skills_root: Path, skill_name: str) -> Path:
    relative = Path(skill_name)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise ValueError("invalid skill name")
    return skills_root / relative / "SKILL.md"


def _skill_payload(skill_file: Path, skill_name: str) -> dict[str, str]:
    payload = {
        "path": str(skill_file),
        "name": skill_name,
        "dir_path": str(skill_file.parent),
    }
    if skill_file.parent.is_symlink():
        payload["source_dir_path"] = str(skill_file.parent.resolve())
    if skill_file.parent.is_symlink() or skill_file.is_symlink():
        payload["source_path"] = str(skill_file.resolve())
    return payload


def _read_url_text(url: str, *, timeout: int = 15) -> str:
    with urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _fetch_printing_press_registry() -> dict:
    global _PRINTING_PRESS_REGISTRY_CACHE
    global _PRINTING_PRESS_REGISTRY_CACHE_EXPIRES_AT

    now = time.monotonic()
    if (
        _PRINTING_PRESS_REGISTRY_CACHE is not None
        and _PRINTING_PRESS_REGISTRY_CACHE_EXPIRES_AT > now
    ):
        return _PRINTING_PRESS_REGISTRY_CACHE

    registry = json.loads(_read_url_text(PRINTING_PRESS_REGISTRY_URL))
    _PRINTING_PRESS_REGISTRY_CACHE = registry
    _PRINTING_PRESS_REGISTRY_CACHE_EXPIRES_AT = (
        now + PRINTING_PRESS_REGISTRY_CACHE_SECONDS
    )
    return registry


def _printing_press_skill_name(name: str) -> str:
    normalized = name.strip().lower()
    if not PRINTING_PRESS_SKILL_NAME_RE.fullmatch(normalized):
        raise ValueError("invalid printing press skill name")
    return f"pp-{normalized}"


def _printing_press_skill_installed_targets(name: str) -> dict[str, bool]:
    skill_name = _printing_press_skill_name(name)
    return {
        scope: _skill_file(_skills_dir(None, scope), skill_name).is_file()
        for scope in sorted(PRINTING_PRESS_TARGET_SCOPES)
    }


def _printing_press_entry_payload(entry: dict) -> dict:
    name = str(entry.get("name", ""))
    release = entry.get("release") if isinstance(entry.get("release"), dict) else {}
    creator = entry.get("creator") if isinstance(entry.get("creator"), dict) else {}
    mcp = entry.get("mcp") if isinstance(entry.get("mcp"), dict) else None
    payload = {
        "name": name,
        "skill_name": _printing_press_skill_name(name),
        "category": entry.get("category") or "other",
        "api": entry.get("api") or name,
        "description": entry.get("description") or "",
        "path": entry.get("path") or "",
        "release": {
            "cli_name": release.get("cli_name") or "",
            "version": release.get("version") or "",
            "released_at": release.get("released_at") or "",
        },
        "printer": entry.get("printer") or creator.get("handle") or "",
        "printer_name": entry.get("printer_name") or creator.get("name") or "",
        "creator": {
            "handle": creator.get("handle") or "",
            "name": creator.get("name") or "",
        },
        "installed_targets": _printing_press_skill_installed_targets(name),
    }
    if mcp:
        payload["mcp"] = {
            "binary": mcp.get("binary") or "",
            "transports": mcp.get("transports") or [],
            "tool_count": mcp.get("tool_count") or 0,
            "public_tool_count": mcp.get("public_tool_count") or 0,
            "auth_type": mcp.get("auth_type") or "",
            "env_vars": mcp.get("env_vars") or [],
            "mcp_ready": mcp.get("mcp_ready") or "",
            "spec_format": mcp.get("spec_format") or "",
        }
    return payload


def _printing_press_entries(
    registry: dict, *, query: str = "", category: str = ""
) -> list[dict]:
    entries = registry.get("entries")
    if not isinstance(entries, list):
        return []
    normalized_query = query.strip().lower()
    normalized_category = category.strip().lower()
    filtered = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if (
            normalized_category
            and str(entry.get("category", "")).lower() != normalized_category
        ):
            continue
        if normalized_query:
            search_terms = entry.get("search_terms")
            search_values = search_terms if isinstance(search_terms, list) else []
            searchable = [
                str(entry.get("name", "")),
                str(entry.get("api", "")),
                str(entry.get("description", "")),
                *[str(term) for term in search_values],
            ]
            if not any(normalized_query in value.lower() for value in searchable):
                continue
        try:
            filtered.append(_printing_press_entry_payload(entry))
        except ValueError:
            continue
    return sorted(filtered, key=lambda item: (item["category"], item["name"]))


def _printing_press_registry_has_entry(registry: dict, name: str) -> bool:
    entries = registry.get("entries")
    if not isinstance(entries, list):
        return False
    return any(
        isinstance(entry, dict) and str(entry.get("name", "")).lower() == name
        for entry in entries
    )


def _printing_press_catalog_payload(*, query: str = "", category: str = "") -> dict:
    registry = _fetch_printing_press_registry()
    all_entries = _printing_press_entries(registry)
    filtered_entries = _printing_press_entries(registry, query=query, category=category)
    category_counts: dict[str, int] = {}
    for entry in all_entries:
        category_name = entry["category"]
        category_counts[category_name] = category_counts.get(category_name, 0) + 1
    categories = [
        {"name": name, "count": count}
        for name, count in sorted(category_counts.items(), key=lambda item: item[0])
    ]
    return {
        "schema_version": registry.get("schema_version"),
        "source_url": PRINTING_PRESS_REGISTRY_URL,
        "categories": categories,
        "entries": filtered_entries,
    }


def _fetch_printing_press_skill_content(name: str) -> str:
    skill_name = _printing_press_skill_name(name).removeprefix("pp-")
    return _read_url_text(PRINTING_PRESS_SKILL_URL_TEMPLATE.format(name=skill_name))


def _install_printing_press_skill(
    *, name: str, target_scope: str, content: str
) -> dict[str, str | bool]:
    if target_scope not in PRINTING_PRESS_TARGET_SCOPES:
        raise ValueError("invalid target scope")
    skill_name = _printing_press_skill_name(name)
    skill_file = _skill_file(_skills_dir(None, target_scope), skill_name)
    skill_dir = skill_file.parent
    if skill_file.is_file():
        return {
            "target": target_scope,
            "status": "already_installed",
            "path": str(skill_file),
            "created": False,
        }
    if skill_dir.exists() or skill_dir.is_symlink():
        raise FileExistsError(f"Skill '{skill_name}' already exists in {skill_dir}")
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(content, encoding="utf-8")
    return {
        "target": target_scope,
        "status": "installed",
        "path": str(skill_file),
        "created": True,
    }


def _same_resolved_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=True) == right.resolve(strict=True)
    except FileNotFoundError:
        return left.resolve(strict=False) == right.resolve(strict=False)


def _symlink_skill_between_scopes(
    *,
    skill_name: str,
    source_scope: str,
    target_scope: str,
) -> dict[str, str | bool]:
    source_root = _skills_dir(None, source_scope)
    target_root = _skills_dir(None, target_scope)
    source_skill_file = _skill_file(source_root, skill_name)
    target_skill_file = _skill_file(target_root, skill_name)
    source_dir = source_skill_file.parent
    target_dir = target_skill_file.parent

    if source_scope == target_scope:
        raise ValueError("source and target scopes must differ")
    if not source_skill_file.is_file():
        raise FileNotFoundError(f"Skill '{skill_name}' was not found in {source_root}")

    source_link_target = source_dir
    if target_dir.exists() or target_dir.is_symlink():
        if target_dir.is_symlink() and _same_resolved_path(
            target_dir, source_link_target
        ):
            return {
                "name": skill_name,
                "created": False,
                "source_scope": source_scope,
                "target_scope": target_scope,
                "source_dir": str(source_link_target),
                "target_dir": str(target_dir),
                "target_path": str(target_skill_file),
            }
        raise FileExistsError(f"Skill '{skill_name}' already exists in {target_root}")

    target_root.mkdir(parents=True, exist_ok=True)
    target_dir.symlink_to(source_link_target, target_is_directory=True)
    return {
        "name": skill_name,
        "created": True,
        "source_scope": source_scope,
        "target_scope": target_scope,
        "source_dir": str(source_link_target),
        "target_dir": str(target_dir),
        "target_path": str(target_skill_file),
    }


def _auto_link_personal_skills_sync() -> dict:
    return skills_autolink.sync_auto_linked_skills()


def _auto_link_settings_payload(*, sync: bool = False) -> dict:
    sync_result = _auto_link_personal_skills_sync() if sync else None
    return {
        "auto_link_personal_skills": dispatcher_config.auto_link_personal_skills(),
        "personal_skills_dir": str(_home_skills_dir()),
        "codex_skills_dir": str(_codex_skills_dir()),
        "claude_skills_dir": str(_claude_skills_dir()),
        "config_path": str(dispatcher_config.CODEX_DISPATCHER_CONFIG_PATH),
        "config_exists": dispatcher_config.CODEX_DISPATCHER_CONFIG_PATH.is_file(),
        "sync": sync_result,
    }


@api_view(["GET", "POST"])
def skills_list(request):
    """List skills or create a new one.

    Query params:
        path: project directory (omit for global skills)
    """
    project_path = request.query_params.get("path", "").strip() or None
    scope = request.query_params.get("scope", "home").strip()
    try:
        skills_root = _skills_dir(project_path, scope)
    except ValueError:
        return Response(
            {"error": "invalid skill scope"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if request.method == "POST":
        name = request.data.get("name", "").strip()
        if not name:
            return Response(
                {"error": "name is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            skill_file = _skill_file(skills_root, name)
        except ValueError:
            return Response(
                {"error": "invalid skill name"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if skill_file.exists():
            return Response(
                {"error": f"Skill '{name}' already exists"},
                status=status.HTTP_409_CONFLICT,
            )
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        content = request.data.get(
            "content", f"---\nname: {name}\ndescription: \n---\n\n"
        )
        skill_file.write_text(content, encoding="utf-8")
        return Response(
            {"name": name, "path": str(skill_file)},
            status=status.HTTP_201_CREATED,
        )

    if project_path:
        return Response(
            {"skills": _list_skill_entries(skills_root), "skills_dir": str(skills_root)}
        )

    auto_link_sync = (
        _auto_link_personal_skills_sync()
        if dispatcher_config.auto_link_personal_skills()
        else None
    )

    sections = []
    for scope_info in _skill_scope_payload():
        root = Path(scope_info["skills_dir"])
        sections.append({**scope_info, "skills": _list_skill_entries(root)})
    return Response(
        {
            "skills": sections[0]["skills"],
            "skills_dir": sections[0]["skills_dir"],
            "sections": sections,
            "auto_link_personal_skills": _auto_link_settings_payload(),
            "auto_link_personal_skills_sync": auto_link_sync,
        }
    )


@api_view(["GET", "PATCH", "POST"])
def skills_auto_link_settings(request):
    if request.method == "GET":
        return Response(_auto_link_settings_payload())

    if request.method == "PATCH":
        if "auto_link_personal_skills" not in request.data:
            return Response(
                {"error": "auto_link_personal_skills is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        dispatcher_config.set_auto_link_personal_skills(
            bool(request.data.get("auto_link_personal_skills"))
        )
        sync = dispatcher_config.auto_link_personal_skills()
        return Response(_auto_link_settings_payload(sync=sync))

    return Response(_auto_link_settings_payload(sync=True))


@api_view(["POST"])
def skills_symlink(request):
    """Symlink a global skill between the personal home and Openbase agent homes."""
    skill_name = request.data.get("name", "").strip()
    source_scope = request.data.get("source_scope", "").strip()
    target_scope = request.data.get("target_scope", "").strip()
    if not skill_name or not source_scope or not target_scope:
        return Response(
            {"error": "name, source_scope, and target_scope are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if (
        source_scope not in GLOBAL_SKILL_SCOPES
        or target_scope not in GLOBAL_SKILL_SCOPES
    ):
        return Response(
            {"error": "invalid skill scope"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        _skill_file(_skills_dir(None, source_scope), skill_name)
        result = _symlink_skill_between_scopes(
            skill_name=skill_name,
            source_scope=source_scope,
            target_scope=target_scope,
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except FileNotFoundError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    except FileExistsError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

    return Response(
        result,
        status=status.HTTP_201_CREATED if result["created"] else status.HTTP_200_OK,
    )


@api_view(["GET"])
def printing_press_catalog(request):
    query = request.query_params.get("q", "")
    category = request.query_params.get("category", "")
    try:
        return Response(_printing_press_catalog_payload(query=query, category=category))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return Response(
            {"error": f"Unable to load Printing Press catalog: {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )


@api_view(["POST"])
def printing_press_install(request):
    name = request.data.get("name", "")
    raw_targets = request.data.get("targets", [])
    if not name:
        return Response(
            {"error": "name is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not isinstance(raw_targets, list) or not raw_targets:
        return Response(
            {"error": "targets must be a non-empty list"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    targets = list(dict.fromkeys(str(target).strip() for target in raw_targets))
    if any(target not in PRINTING_PRESS_TARGET_SCOPES for target in targets):
        return Response(
            {"error": "invalid target scope"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        _printing_press_skill_name(name)
        normalized_name = name.strip().lower()
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    try:
        registry = _fetch_printing_press_registry()
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return Response(
            {"error": f"Unable to load Printing Press catalog: {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    if not _printing_press_registry_has_entry(registry, normalized_name):
        return Response(
            {"error": f"Printing Press skill '{normalized_name}' was not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    installed_targets = _printing_press_skill_installed_targets(name)
    needs_content = any(not installed_targets[target] for target in targets)
    try:
        content = _fetch_printing_press_skill_content(name) if needs_content else ""
    except (HTTPError, URLError, TimeoutError) as exc:
        return Response(
            {"error": f"Unable to load Printing Press skill: {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    results = []
    for target in targets:
        try:
            results.append(
                _install_printing_press_skill(
                    name=name,
                    target_scope=target,
                    content=content,
                )
            )
        except FileExistsError as exc:
            results.append({"target": target, "status": "conflict", "error": str(exc)})
        except OSError as exc:
            results.append({"target": target, "status": "error", "error": str(exc)})

    has_error = any(result["status"] in {"conflict", "error"} for result in results)
    response_status = status.HTTP_207_MULTI_STATUS if has_error else status.HTTP_200_OK
    if all(result["status"] == "installed" for result in results):
        response_status = status.HTTP_201_CREATED
    return Response(
        {
            "name": name,
            "skill_name": _printing_press_skill_name(name),
            "results": results,
        },
        status=response_status,
    )


@api_view(["GET", "PUT", "DELETE"])
def skill_detail(request, skill_name):
    """Read, write, or delete a single skill's SKILL.md.

    Query params:
        path: project directory (omit for global skills)
    """
    project_path = request.query_params.get("path", "").strip() or None
    scope = request.query_params.get("scope", "home").strip()
    try:
        skill_file = _skill_file(_skills_dir(project_path, scope), skill_name)
    except ValueError:
        return Response(
            {"error": "invalid skill name or scope"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if request.method == "DELETE":
        skill_dir = skill_file.parent
        if not skill_dir.is_dir():
            return Response(
                {"error": f"Skill '{skill_name}' not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        if skill_dir.is_symlink():
            skill_dir.unlink()
        else:
            shutil.rmtree(skill_dir)
        return Response({"success": True})

    if request.method == "PUT":
        content = request.data.get("content", "")
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(content, encoding="utf-8")
        return Response({"content": content, "path": str(skill_file)})

    # GET
    if skill_file.exists():
        content = skill_file.read_text(encoding="utf-8")
    else:
        content = ""
    return Response({"content": content, **_skill_payload(skill_file, skill_name)})
