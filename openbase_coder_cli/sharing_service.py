"""Publish local report files to Openbase Cloud sharing and keep them fresh.

Sharing is account-gated on the cloud side (explicit per-email grants, no
public links). This module is the owner-device half: it reads a report from
`.reports/`, bundles the image assets its markdown references, and publishes
revisions over the user's authenticated cloud session. The local file stays
the source of truth — publishing is outbound-only and idempotent (the cloud
skips a new revision when content is unchanged), so republishing is always
safe.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from openbase_coder_cli.reports_service import (
    REPORTS_IMAGE_EXTENSIONS,
    REPORTS_MAX_IMAGE_BYTES,
    REPORTS_MAX_TEXT_BYTES,
    _report_markdown_title,
    _resolve_reports_path,
)
from openbase_coder_cli.services import cloud_sharing
from openbase_coder_cli.services.cloud_registration import local_device_id

MAX_SHARE_ASSETS = 20
# Mirror of the cloud's publish budget: the whole request must fit Django's
# 2.5 MB body ceiling, so assets are capped at 1 MiB each and 1 MiB total
# (raw bytes; base64 inflates by 4/3). Oversized images are skipped -- the
# report still shares, the viewer shows a placeholder.
MAX_SHARE_ASSET_BYTES = 1 * 1024 * 1024
MAX_SHARE_TOTAL_ASSET_BYTES = 1 * 1024 * 1024
SHARE_SWEEP_DEBOUNCE_SECONDS = 300.0
SWEEP_MAX_PUBLISHES_PER_PASS = 100

MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(<?([^)>\s]+)>?\)")

# Likely-credential patterns checked before any content leaves the device.
# Findings report the rule and line only -- never the matched text.
SECRET_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("sk-style-api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
    ),
    (
        "secret-assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*"
            r"['\"]?[A-Za-z0-9_\-/+=]{16,}"
        ),
    ),
]

_cache_lock = threading.Lock()
_shared_origin_keys: set[str] = set()
_last_published_hashes: dict[str, str] = {}
_last_sweep_monotonic: float | None = None


def scan_for_secrets(content: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        for rule, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append({"rule": rule, "line": line_number})
                break
    return findings


def _resolved_project_path(project_path: str) -> str:
    return str(Path(project_path).expanduser().resolve())


def _normalized_relative_path(relative_path: str) -> str:
    return os.path.normpath(relative_path.strip()).replace(os.sep, "/")


def report_origin_key(project_path: str, relative_path: str) -> str:
    """Mirror of the cloud's SharedItem.derive_origin_key for kind=report."""
    raw = "\n".join(
        [
            "report",
            _resolved_project_path(project_path),
            _normalized_relative_path(relative_path),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def collect_report_assets(
    project_path: str, relative_path: str, content: str
) -> list[dict[str, Any]]:
    """Bundle image files the report's markdown references, reports-dir relative."""
    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_bytes = 0
    report_dir = os.path.dirname(_normalized_relative_path(relative_path))
    for match in MARKDOWN_IMAGE_RE.finditer(content):
        ref = match.group(1)
        if ref.startswith(("http://", "https://", "data:", "/", "#")):
            continue
        normalized = os.path.normpath(os.path.join(report_dir, ref)).replace(
            os.sep, "/"
        )
        if normalized in seen:
            continue
        try:
            asset_path, _reports_dir = _resolve_reports_path(project_path, normalized)
        except ValueError:
            continue
        if not asset_path.is_file():
            continue
        if asset_path.suffix.lower() not in REPORTS_IMAGE_EXTENSIONS:
            continue
        size = asset_path.stat().st_size
        if size > min(REPORTS_MAX_IMAGE_BYTES, MAX_SHARE_ASSET_BYTES):
            continue
        if total_bytes + size > MAX_SHARE_TOTAL_ASSET_BYTES:
            continue
        total_bytes += size
        seen.add(normalized)
        content_type = mimetypes.guess_type(asset_path.name)[0] or ""
        assets.append(
            {
                "path": normalized,
                "content_type": content_type,
                "body_base64": base64.b64encode(asset_path.read_bytes()).decode(
                    "ascii"
                ),
            }
        )
        if len(assets) >= MAX_SHARE_ASSETS:
            break
    return assets


def build_publish_payload(project_path: str, relative_path: str) -> dict[str, Any]:
    resolved_project = _resolved_project_path(project_path)
    normalized = _normalized_relative_path(relative_path)
    file_path, _reports_dir = _resolve_reports_path(resolved_project, normalized)
    if not file_path.is_file():
        raise FileNotFoundError(normalized)
    if file_path.stat().st_size > REPORTS_MAX_TEXT_BYTES:
        raise ValueError("Report is too large to share")
    content = file_path.read_text(encoding="utf-8", errors="replace")
    title = _report_markdown_title(file_path) or file_path.name
    return {
        "kind": "report",
        "title": title,
        "origin_project_path": resolved_project,
        "origin_item_path": normalized,
        "origin_device_id": local_device_id(),
        "content": content,
        "assets": collect_report_assets(resolved_project, normalized, content),
    }


def publish_report(
    project_path: str, relative_path: str, *, allow_secrets: bool = False
) -> dict[str, Any]:
    payload = build_publish_payload(project_path, relative_path)
    findings = scan_for_secrets(payload["content"])
    if findings and not allow_secrets:
        return {
            "ok": False,
            "reason": "possible_secrets",
            "findings": findings,
        }
    result = cloud_sharing.publish_item(payload)
    if not result.ok:
        return {"ok": False, "error": result.error}
    key = report_origin_key(project_path, relative_path)
    with _cache_lock:
        _shared_origin_keys.add(key)
        _last_published_hashes[key] = hashlib.sha256(
            payload["content"].encode("utf-8")
        ).hexdigest()
    response = result.response if isinstance(result.response, dict) else {}
    return {
        "ok": True,
        "id": response.get("id"),
        "revision_seq": response.get("revision_seq"),
    }


def get_share_state(project_path: str, relative_path: str) -> dict[str, Any]:
    key = report_origin_key(project_path, relative_path)
    result = cloud_sharing.list_items(origin_key=key)
    if not result.ok:
        return {"shared": False, "error": result.error}
    items = result.response if isinstance(result.response, list) else []
    if not items:
        with _cache_lock:
            _shared_origin_keys.discard(key)
        return {"shared": False}
    item = items[0]
    with _cache_lock:
        _shared_origin_keys.add(key)
    state: dict[str, Any] = {"shared": True, "item": item, "grants": []}
    grants_result = cloud_sharing.list_grants(item["id"])
    if grants_result.ok and isinstance(grants_result.response, dict):
        state["grants"] = grants_result.response.get("grants") or []
    return state


def unshare_report(project_path: str, relative_path: str) -> dict[str, Any]:
    state = get_share_state(project_path, relative_path)
    if not state.get("shared"):
        return {"ok": True, "shared": False}
    result = cloud_sharing.delete_item(state["item"]["id"])
    if not result.ok:
        return {"ok": False, "error": result.error}
    with _cache_lock:
        _shared_origin_keys.discard(report_origin_key(project_path, relative_path))
    return {"ok": True, "shared": False}


def maybe_republish_report(project_path: str, relative_path: str) -> None:
    """Republish after a local edit, but only for reports known to be shared."""
    key = report_origin_key(project_path, relative_path)
    with _cache_lock:
        known = key in _shared_origin_keys
    if known:
        publish_report(project_path, relative_path)


def sync_shared_reports(*, force: bool = False) -> None:
    """Debounced background republish of every share whose file lives here.

    Keeps shares fresh when agents rewrite report files directly on disk
    (bypassing the local reports API). Publishing is idempotent, so
    republishing unchanged reports costs one skipped-revision round trip.
    """
    global _last_sweep_monotonic
    with _cache_lock:
        now = time.monotonic()
        if (
            not force
            and _last_sweep_monotonic is not None
            and now - _last_sweep_monotonic < SHARE_SWEEP_DEBOUNCE_SECONDS
        ):
            return
        _last_sweep_monotonic = now

    result = cloud_sharing.list_items()
    if not result.ok or not isinstance(result.response, list):
        return
    local_keys: set[str] = set()
    published = 0
    for item in result.response:
        project_path = item.get("origin_project_path") or ""
        relative_path = item.get("origin_item_path") or ""
        if not project_path or not relative_path:
            continue
        try:
            file_path, _reports_dir = _resolve_reports_path(project_path, relative_path)
        except ValueError:
            continue
        if not file_path.is_file():
            continue
        key = report_origin_key(project_path, relative_path)
        local_keys.add(key)
        if published >= SWEEP_MAX_PUBLISHES_PER_PASS:
            continue
        content_hash = hashlib.sha256(
            file_path.read_text(encoding="utf-8", errors="replace").encode("utf-8")
        ).hexdigest()
        with _cache_lock:
            unchanged = _last_published_hashes.get(key) == content_hash
        if unchanged:
            continue
        # Secret findings block the auto-republish (fail closed); the owner
        # re-shares manually through the dialog to override.
        outcome = publish_report(project_path, relative_path)
        if outcome.get("ok"):
            published += 1
    with _cache_lock:
        _shared_origin_keys.clear()
        _shared_origin_keys.update(local_keys)


def sync_shared_reports_in_background() -> None:
    threading.Thread(
        target=sync_shared_reports, name="sharing-sweep", daemon=True
    ).start()
