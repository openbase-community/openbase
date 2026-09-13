"""Producers that materialize notifications from reports, approvals, and sync conflicts.

The sweep is pull-driven: it runs before notification list requests, on
``ws/notifications/`` connects, and on a periodic tick while any socket is
open. Thread-completion notifications are produced separately at the event
source (``thread_sync.session_manager``), not here.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from asgiref.sync import async_to_sync

from openbase_coder_cli import sharing_service
from openbase_coder_cli.openbase_coder_cli_app import notification_store
from openbase_coder_cli.openbase_coder_cli_app.notification_store import (
    KIND_APPROVAL,
    KIND_REPORT,
    KIND_SYNC_CONFLICT,
    KIND_THREAD,
)

logger = logging.getLogger(__name__)

SWEEP_DEBOUNCE_SECONDS = 5.0
MAX_PUSH_BODY_LENGTH = 140

_sweep_lock = threading.Lock()
_last_sweep_monotonic: float | None = None


def sync_notification_producers(*, force: bool = False) -> None:
    """Materialize notifications for new reports, approvals, and conflicts."""
    global _last_sweep_monotonic
    with _sweep_lock:
        now = time.monotonic()
        if (
            not force
            and _last_sweep_monotonic is not None
            and now - _last_sweep_monotonic < SWEEP_DEBOUNCE_SECONDS
        ):
            return
        _last_sweep_monotonic = now

    for producer in (_sweep_reports, _sweep_approvals, _sweep_sync_conflicts):
        try:
            producer()
        except Exception:
            logger.exception("Notification producer %s failed", producer.__name__)

    # Piggyback on the same pull-driven tick to keep cloud shares fresh when
    # agents rewrite shared report files directly on disk. Runs on its own
    # thread with its own debounce, so this never adds request latency.
    sharing_service.sync_shared_reports_in_background()


def notify_thread_turn_finished(
    thread_id: str,
    *,
    title: str,
    body: str,
) -> None:
    """Record a thread-finished notification for a manual thread.

    Callers have already checked ``is_manual_thread``. A completion on an
    already-read thread notification reopens it ("unread again"), which is
    what makes the notification exist iff the latest result is unread.
    """
    entry = notification_store.upsert_notification(
        KIND_THREAD,
        thread_id,
        title=title,
        body=_truncate(body),
        thread_id=thread_id,
    )
    if entry:
        _push_in_background(entry)


def _sweep_reports() -> None:
    from openbase_coder_cli.reports_service import list_report_items

    items = list_report_items()
    if not items:
        return
    newest_mtime = max(float(item.get("updated_at") or 0) for item in items)
    watermark, baselined = notification_store.get_report_watermark()
    if not baselined:
        # First run: never flood with pre-existing reports.
        notification_store.set_report_watermark(newest_mtime)
        return
    threshold = watermark or 0.0
    fresh = [item for item in items if float(item.get("updated_at") or 0) > threshold]
    for item in fresh:
        file_payload = item.get("file") or {}
        project_payload = item.get("project") or {}
        project_path = str(project_payload.get("path") or "")
        title = file_payload.get("title") or file_payload.get("name") or "New report"
        entry = notification_store.upsert_notification(
            KIND_REPORT,
            str(item["id"]),
            title=str(title),
            body=_truncate(project_path.rsplit("/", 1)[-1] if project_path else ""),
            project_path=project_path or None,
        )
        if entry:
            _push_in_background(entry)
    if newest_mtime > threshold:
        notification_store.set_report_watermark(newest_mtime)


def _sweep_approvals() -> None:
    from openbase_coder_cli.openbase_coder_cli_app.approvals import (
        pending_approval_requests,
    )

    requests = async_to_sync(pending_approval_requests)()
    pending_ids: set[str] = set()
    for request in requests:
        request_id = str(request.get("id") or "").strip()
        if not request_id:
            continue
        pending_ids.add(request_id)
        entry = notification_store.upsert_notification(
            KIND_APPROVAL,
            request_id,
            title="Approval requested",
            body=_truncate(_approval_summary(request)),
            thread_id=_approval_thread_id(request),
            reopen_if_read=False,
        )
        if entry:
            _push_in_background(entry)
    # Approvals answered (or expired) on any device stop being pending;
    # resolve their notifications so they clear everywhere.
    for entity_id in notification_store.unresolved_ids(KIND_APPROVAL) - pending_ids:
        notification_store.resolve_notification(KIND_APPROVAL, entity_id)


def _sweep_sync_conflicts() -> None:
    """Notify for cross-device thread snapshot conflicts (both backends).

    Same source the Sync Conflicts surfaces poll (settings/thread-sync/
    conflicts/), so resolving a conflict anywhere clears the notification.
    """
    conflicts = _thread_sync_conflicts()
    live_ids: set[str] = set()
    for backend, conflict in conflicts:
        conflict_id = str(conflict.get("id") or "").strip()
        if not conflict_id:
            continue
        entity_id = f"{backend}:{conflict_id}"
        live_ids.add(entity_id)
        entry = notification_store.upsert_notification(
            KIND_SYNC_CONFLICT,
            entity_id,
            title="Sync conflict",
            body=_truncate(_conflict_summary(conflict)),
            thread_id=_optional_str(conflict.get("thread_id")),
            reopen_if_read=False,
        )
        if entry:
            _push_in_background(entry)
    for entity_id in notification_store.unresolved_ids(KIND_SYNC_CONFLICT) - live_ids:
        notification_store.resolve_notification(KIND_SYNC_CONFLICT, entity_id)


def _thread_sync_conflicts() -> list[tuple[str, dict[str, Any]]]:
    from openbase_coder_cli.thread_sync.claude_conflict_payloads import (
        claude_thread_snapshot_conflicts_payload,
    )
    from openbase_coder_cli.thread_sync.thread_exchange import (
        thread_snapshot_conflicts_payload,
    )

    pairs: list[tuple[str, dict[str, Any]]] = []
    for backend, payload in (
        ("codex", thread_snapshot_conflicts_payload()),
        ("claude", claude_thread_snapshot_conflicts_payload()),
    ):
        raw_conflicts = payload.get("conflicts")
        if isinstance(raw_conflicts, list):
            pairs.extend(
                (backend, conflict)
                for conflict in raw_conflicts
                if isinstance(conflict, dict)
            )
    return pairs


def _approval_thread_id(request: dict[str, Any]) -> str | None:
    params = request.get("params")
    for source in (request, params if isinstance(params, dict) else {}):
        for key in ("thread_id", "threadId"):
            value = _optional_str(source.get(key))
            if value:
                return value
    return None


def _approval_summary(request: dict[str, Any]) -> str:
    params = request.get("params")
    sources: list[dict[str, Any]] = [request]
    if isinstance(params, dict):
        sources.insert(0, params)
    for source in sources:
        for key in ("description", "command", "action", "skill", "method"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "An agent is waiting for your approval."


def _conflict_summary(conflict: dict[str, Any]) -> str:
    title = _optional_str(conflict.get("title")) or _optional_str(
        conflict.get("thread_id")
    )
    device = _optional_str(conflict.get("source_device_name"))
    if title and device:
        return f"{title} diverged from {device}"
    if title:
        return f"{title} diverged on another device"
    return "A thread diverged across your devices."


def _push_in_background(entry: dict[str, Any]) -> None:
    """Relay the notification to Openbase Cloud (APNs/FCM) off-thread.

    Strictly fire-and-forget: local notification behavior must be identical
    when the cloud is unreachable or the relay endpoint is not deployed.
    """
    thread = threading.Thread(
        target=_send_push,
        args=(dict(entry),),
        name="notification-push",
        daemon=True,
    )
    thread.start()


def _send_push(entry: dict[str, Any]) -> None:
    from openbase_coder_cli.config.cloud_notifications import send_notification_push

    try:
        send_notification_push(
            title=str(entry.get("title") or "Openbase"),
            body=str(entry.get("body") or ""),
            user_info=_push_user_info(entry),
        )
    except Exception as exc:
        logger.info("Cloud notification push skipped: %s", exc)


def _push_user_info(entry: dict[str, Any]) -> dict[str, str]:
    kind = entry.get("kind")
    user_info: dict[str, str] = {"notification_id": str(entry.get("id") or "")}
    if kind == KIND_THREAD:
        user_info["openbase_destination"] = "threads"
        user_info["thread_id"] = str(entry.get("thread_id") or "")
    elif kind == KIND_REPORT:
        user_info["openbase_destination"] = "reports"
        user_info["report_id"] = str(entry.get("entity_id") or "")
    elif kind == KIND_APPROVAL:
        user_info["openbase_destination"] = "approvals"
        user_info["approval_request_id"] = str(entry.get("entity_id") or "")
    elif kind == KIND_SYNC_CONFLICT:
        user_info["openbase_destination"] = "sync_conflicts"
    return {key: value for key, value in user_info.items() if value}


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _truncate(value: str, limit: int = MAX_PUSH_BODY_LENGTH) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
