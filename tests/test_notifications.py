from __future__ import annotations

import os
import time
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

import pytest  # noqa: E402

from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    notification_producers,
    notification_store,
    notifications,
    thread_origins,
)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(notification_producers, "_last_sweep_monotonic", None)
    yield


@pytest.fixture(autouse=True)
def _no_cloud_push(monkeypatch):
    pushed: list[dict] = []
    monkeypatch.setattr(
        notification_producers,
        "_push_in_background",
        lambda entry: pushed.append(entry),
    )
    yield pushed


@pytest.fixture(autouse=True)
def _empty_producer_sources(monkeypatch):
    """Keep sweeps off real machine state; tests override what they need."""

    async def no_pending():
        return []

    monkeypatch.setattr(
        "openbase_coder_cli.reports_service.list_report_items", lambda: []
    )
    monkeypatch.setattr(
        "openbase_coder_cli.openbase_coder_cli_app.approvals.pending_approval_requests",
        no_pending,
    )
    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.thread_exchange.thread_snapshot_conflicts_payload",
        lambda: {"conflicts": []},
    )
    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.claude_conflict_payloads.claude_thread_snapshot_conflicts_payload",
        lambda: {"conflicts": []},
    )
    yield


# --- store ---


def test_upsert_list_and_unread_count():
    created = notification_store.upsert_notification(
        "thread", "t-1", title="My thread", body="Done", thread_id="t-1"
    )
    assert created is not None
    assert created["id"] == "thread:t-1"

    duplicate = notification_store.upsert_notification(
        "thread", "t-1", title="My thread", body="Done again"
    )
    assert duplicate is None  # already live and unread

    payload = notification_store.list_notifications()
    assert payload["unread_count"] == 1
    assert [entry["id"] for entry in payload["notifications"]] == ["thread:t-1"]


def test_mark_read_and_reopen_on_new_completion():
    notification_store.upsert_notification("thread", "t-1", title="T", body="")
    assert notification_store.mark_read(kind="thread", entity_id="t-1") == 1
    assert notification_store.list_notifications()["unread_count"] == 0

    reopened = notification_store.upsert_notification(
        "thread", "t-1", title="T", body="new result"
    )
    assert reopened is not None
    assert notification_store.list_notifications()["unread_count"] == 1


def test_reopen_if_read_false_stays_read():
    notification_store.upsert_notification(
        "approval", "a-1", title="A", reopen_if_read=False
    )
    notification_store.mark_read(kind="approval", entity_id="a-1")
    assert (
        notification_store.upsert_notification(
            "approval", "a-1", title="A", reopen_if_read=False
        )
        is None
    )
    assert notification_store.list_notifications()["unread_count"] == 0


def test_resolve_hides_from_list():
    notification_store.upsert_notification("approval", "a-1", title="A")
    assert notification_store.resolve_notification("approval", "a-1")
    payload = notification_store.list_notifications()
    assert payload["notifications"] == []
    assert payload["unread_count"] == 0


def test_mark_read_by_ids_and_mark_all():
    notification_store.upsert_notification("thread", "t-1", title="T1")
    notification_store.upsert_notification("thread", "t-2", title="T2")
    assert notification_store.mark_read(["thread:t-1"]) == 1
    assert notification_store.mark_all_read() == 1
    assert notification_store.list_notifications()["unread_count"] == 0


def test_invalid_kind_rejected():
    with pytest.raises(ValueError):
        notification_store.upsert_notification("bogus", "x", title="X")


# --- thread origins ---


def test_origin_default_deny():
    assert not thread_origins.is_manual_thread("unknown-thread")
    thread_origins.set_thread_origin("t-1", thread_origins.MANUAL_ORIGIN)
    assert thread_origins.is_manual_thread("t-1")
    assert not thread_origins.is_manual_thread("t-2")


# --- report sweep ---


def _report_item(item_id: str, mtime: float, title: str = "Report") -> dict:
    project_path, _, rel = item_id.partition(":")
    return {
        "id": item_id,
        "project": {"path": project_path},
        "file": {"path": rel, "name": rel, "title": title},
        "updated_at": mtime,
    }


def test_report_sweep_baselines_then_notifies(monkeypatch):
    now = time.time()
    items = [_report_item("/proj:old.md", now - 100)]
    monkeypatch.setattr(
        "openbase_coder_cli.reports_service.list_report_items", lambda: items
    )

    notification_producers.sync_notification_producers(force=True)
    assert notification_store.list_notifications()["notifications"] == []

    items.insert(0, _report_item("/proj:new.md", now, title="Fresh report"))
    notification_producers.sync_notification_producers(force=True)

    payload = notification_store.list_notifications()
    ids = [entry["id"] for entry in payload["notifications"]]
    assert ids == ["report:/proj:new.md"]
    assert payload["notifications"][0]["title"] == "Fresh report"

    # Re-sweep must not duplicate or reopen.
    notification_store.mark_all_read()
    notification_producers.sync_notification_producers(force=True)
    assert notification_store.list_notifications()["unread_count"] == 0


# --- approval sweep ---


def test_approval_sweep_creates_and_resolves(monkeypatch):
    requests = [
        {
            "id": "appr-1",
            "method": "openbaseSkill/requestApproval",
            "params": {"description": "Approve the thing", "threadId": "t-9"},
        }
    ]

    async def fake_pending():
        return requests

    monkeypatch.setattr(
        "openbase_coder_cli.openbase_coder_cli_app.approvals.pending_approval_requests",
        fake_pending,
    )
    monkeypatch.setattr(
        "openbase_coder_cli.reports_service.list_report_items", lambda: []
    )

    notification_producers.sync_notification_producers(force=True)
    payload = notification_store.list_notifications()
    assert payload["unread_count"] == 1
    entry = payload["notifications"][0]
    assert entry["id"] == "approval:appr-1"
    assert entry["body"] == "Approve the thing"
    assert entry["thread_id"] == "t-9"

    requests.clear()  # answered elsewhere
    notification_producers.sync_notification_producers(force=True)
    payload = notification_store.list_notifications()
    assert payload["notifications"] == []
    assert payload["unread_count"] == 0


# --- sync conflict sweep ---


def test_sync_conflict_sweep(monkeypatch):
    conflicts = [
        {
            "id": "device:t-1",
            "thread_id": "t-1",
            "title": "Fix login",
            "source_device_name": "Mac mini",
        }
    ]
    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.thread_exchange.thread_snapshot_conflicts_payload",
        lambda: {"conflicts": conflicts},
    )
    monkeypatch.setattr(
        "openbase_coder_cli.thread_sync.claude_conflict_payloads.claude_thread_snapshot_conflicts_payload",
        lambda: {"conflicts": []},
    )
    monkeypatch.setattr(
        "openbase_coder_cli.reports_service.list_report_items", lambda: []
    )

    notification_producers.sync_notification_producers(force=True)
    payload = notification_store.list_notifications()
    assert [entry["id"] for entry in payload["notifications"]] == [
        "sync_conflict:codex:device:t-1"
    ]
    assert payload["notifications"][0]["body"] == "Fix login diverged from Mac mini"

    conflicts.clear()
    notification_producers.sync_notification_producers(force=True)
    assert notification_store.list_notifications()["notifications"] == []


# --- thread-finished producer ---


def _session_state(**overrides):
    defaults = dict(
        title="Fix the login flow",
        name="my-project",
        directory="/tmp/my-project",
        preview="All done.",
        status=SimpleNamespace(value="idle"),
        queued_turns=[],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_turn_finished_notifies_only_manual_threads():
    from openbase_coder_cli.thread_sync.session_manager import (
        _notify_manual_thread_finished,
    )

    thread_origins.set_thread_origin("manual-1", thread_origins.MANUAL_ORIGIN)

    await _notify_manual_thread_finished("agent-1", _session_state(), failed=False)
    assert notification_store.list_notifications()["notifications"] == []

    await _notify_manual_thread_finished("manual-1", _session_state(), failed=False)
    payload = notification_store.list_notifications()
    assert [entry["id"] for entry in payload["notifications"]] == ["thread:manual-1"]
    assert payload["notifications"][0]["title"] == "Fix the login flow"
    assert payload["notifications"][0]["body"] == "All done."


@pytest.mark.asyncio
async def test_turn_finished_skips_running_or_queued():
    from openbase_coder_cli.thread_sync.session_manager import (
        _notify_manual_thread_finished,
    )

    thread_origins.set_thread_origin("manual-1", thread_origins.MANUAL_ORIGIN)
    await _notify_manual_thread_finished(
        "manual-1",
        _session_state(status=SimpleNamespace(value="running")),
        failed=False,
    )
    await _notify_manual_thread_finished(
        "manual-1",
        _session_state(queued_turns=[{"prompt": "next"}]),
        failed=False,
    )
    assert notification_store.list_notifications()["notifications"] == []


# --- REST views ---


def _get(path: str, params: dict | None = None):
    request = APIRequestFactory().get(path, params or {})
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def _post(path: str, data: dict):
    request = APIRequestFactory().post(path, data, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def test_notification_list_view_runs_sweep(monkeypatch):
    swept = []
    monkeypatch.setattr(
        notifications, "sync_notification_producers", lambda: swept.append(True)
    )
    notification_store.upsert_notification("thread", "t-1", title="T")
    notification_store.upsert_notification("thread", "t-2", title="T2")
    notification_store.mark_read(kind="thread", entity_id="t-2")

    response = notifications.notification_list(_get("/api/notifications/"))
    assert response.status_code == 200
    assert swept == [True]
    assert response.data["unread_count"] == 1
    assert len(response.data["notifications"]) == 2

    response = notifications.notification_list(
        _get("/api/notifications/", {"include_read": "false"})
    )
    assert [entry["id"] for entry in response.data["notifications"]] == ["thread:t-1"]


def test_mark_read_view_by_entity():
    notification_store.upsert_notification("report", "/proj:a.md", title="R")
    response = notifications.notification_mark_read(
        _post(
            "/api/notifications/mark-read/",
            {"kind": "report", "entity_id": "/proj:a.md"},
        )
    )
    assert response.status_code == 200
    assert response.data["marked"] == 1
    assert notification_store.list_notifications()["unread_count"] == 0


def test_mark_read_view_requires_target():
    response = notifications.notification_mark_read(
        _post("/api/notifications/mark-read/", {})
    )
    assert response.status_code == 400


def test_mark_all_read_view():
    notification_store.upsert_notification("thread", "t-1", title="T")
    response = notifications.notification_mark_all_read(
        _post("/api/notifications/mark-all-read/", {})
    )
    assert response.status_code == 200
    assert response.data["marked"] == 1
