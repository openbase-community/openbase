from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from openbase_coder_cli.services.console_settings import (
    get_featured_template_prompt_dismissed,
)
from openbase_coder_cli.services.installation import InstallationConfig

BOILERSYNC_TIMEOUT_SECONDS = 30
BOILERSYNC_CLONE_TIMEOUT_SECONDS = 120
FEATURED_TEMPLATE_REPO_URL = "https://github.com/openbase-community/templates.git"
FEATURED_TEMPLATE_REPO_ORG = "openbase-community"
FEATURED_TEMPLATE_REPO_NAME = "templates"


def _featured_source_payload(sources_payload: dict[str, Any] | None) -> dict[str, Any]:
    sources = sources_payload.get("sources", []) if sources_payload else []
    installed = any(
        source.get("org") == FEATURED_TEMPLATE_REPO_ORG
        and source.get("repo") == FEATURED_TEMPLATE_REPO_NAME
        for source in sources
        if isinstance(source, dict)
    )
    dismissed = get_featured_template_prompt_dismissed()
    return {
        "org": FEATURED_TEMPLATE_REPO_ORG,
        "repo": FEATURED_TEMPLATE_REPO_NAME,
        "repo_url": FEATURED_TEMPLATE_REPO_URL,
        "installed": installed,
        "prompt_dismissed": dismissed,
        "prompt_visible": not installed and not dismissed,
    }


def boilersync_templates_payload(template_ref: str | None = None) -> dict[str, Any]:
    boilersync_bin = resolve_boilersync_binary()
    if not boilersync_bin:
        return {
            "boilersync_available": False,
            "boilersync_path": None,
            "sources": None,
            "templates": None,
            "details": None,
            "featured_source": _featured_source_payload(None),
            "error": "boilersync was not found on PATH.",
        }

    sources_result = run_boilersync_json(
        boilersync_bin, "templates", "sources", "--json"
    )
    templates_result = run_boilersync_json(
        boilersync_bin, "templates", "list", "--json"
    )
    details_result = None
    if template_ref:
        details_result = run_boilersync_json(
            boilersync_bin,
            "templates",
            "details",
            template_ref,
            "--json",
        )

    errors = [
        result["error"]
        for result in (sources_result, templates_result, details_result)
        if result and result["error"]
    ]

    return {
        "boilersync_available": True,
        "boilersync_path": boilersync_bin,
        "sources": sources_result["payload"],
        "templates": templates_result["payload"],
        "details": details_result["payload"] if details_result else None,
        "featured_source": _featured_source_payload(sources_result["payload"]),
        "error": "\n".join(errors) if errors else None,
    }


def add_boilersync_source(repo_url: str) -> None:
    boilersync_bin = resolve_boilersync_binary()
    if not boilersync_bin:
        raise RuntimeError("boilersync was not found on PATH.")

    try:
        result = subprocess.run(
            [boilersync_bin, "templates", "init", repo_url, "--no-input"],
            capture_output=True,
            text=True,
            timeout=BOILERSYNC_CLONE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"boilersync timed out while importing the repository: {exc}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Unable to run boilersync: {exc}") from exc

    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or "boilersync templates init failed."
        )
        raise RuntimeError(detail)


def remove_boilersync_source(org: str, repo: str) -> None:
    boilersync_bin = resolve_boilersync_binary()
    if not boilersync_bin:
        raise RuntimeError("boilersync was not found on PATH.")

    sources_result = run_boilersync_json(
        boilersync_bin, "templates", "sources", "--json"
    )
    if sources_result["error"]:
        raise RuntimeError(str(sources_result["error"]))

    payload = sources_result["payload"]
    if not isinstance(payload, dict):
        raise RuntimeError("boilersync returned an invalid template sources payload.")

    source = next(
        (
            candidate
            for candidate in payload.get("sources", [])
            if isinstance(candidate, dict)
            and candidate.get("org") == org
            and candidate.get("repo") == repo
        ),
        None,
    )
    if source is None:
        raise ValueError(f"Template repository '{org}/{repo}' was not found.")

    template_root = (
        Path(str(payload.get("template_root_dir", ""))).expanduser().resolve()
    )
    source_path = Path(str(source.get("path", ""))).expanduser().resolve()
    expected_path = (template_root / org / repo).resolve()
    if source_path != expected_path or not source_path.is_relative_to(template_root):
        raise RuntimeError(
            "Refusing to remove a template repository outside the template cache."
        )
    if not (source_path / ".git").exists():
        raise RuntimeError(
            "Refusing to remove a directory that is not a template repository."
        )

    shutil.rmtree(source_path)
    org_dir = source_path.parent
    if org_dir.exists() and not any(org_dir.iterdir()):
        org_dir.rmdir()


def run_boilersync_json(boilersync_bin: str, *args: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [boilersync_bin, *args],
            capture_output=True,
            text=True,
            timeout=BOILERSYNC_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return {"payload": None, "error": f"boilersync timed out: {exc}"}
    except OSError as exc:
        return {"payload": None, "error": f"Unable to run boilersync: {exc}"}

    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"boilersync {' '.join(args)} failed."
        )
        return {"payload": None, "error": detail}

    try:
        return {"payload": json.loads(result.stdout), "error": None}
    except json.JSONDecodeError as exc:
        return {
            "payload": None,
            "error": f"Unable to parse boilersync JSON output: {exc}",
        }


def resolve_boilersync_binary() -> str | None:
    for candidate in preferred_boilersync_binary_candidates():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("boilersync")


def preferred_boilersync_binary_candidates() -> list[Path]:
    candidates: list[Path] = []

    if InstallationConfig.exists():
        try:
            workspace = Path(InstallationConfig.load().workspace_path)
        except (OSError, ValueError, TypeError):
            workspace = None
        if workspace is not None:
            candidates.extend(
                [
                    workspace / ".venv" / "bin" / "boilersync",
                    workspace / "cli" / ".venv" / "bin" / "boilersync",
                ]
            )

    candidates.extend(
        [
            Path.home() / ".local" / "bin" / "boilersync",
            Path("/opt/homebrew/bin/boilersync"),
            Path("/usr/local/bin/boilersync"),
        ]
    )

    deduped: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        deduped.append(candidate)
        seen.add(candidate)
    return deduped
