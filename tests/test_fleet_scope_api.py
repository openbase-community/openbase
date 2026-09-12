from __future__ import annotations

# ruff: noqa: E402, I001

import os

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django
from rest_framework.test import APIRequestFactory, force_authenticate

django.setup()

from types import SimpleNamespace

from openbase_coder_cli.openbase_coder_cli_app import reports as report_views
from openbase_coder_cli.openbase_coder_cli_app import threads as thread_views
from openbase_coder_cli.services.fleet_aggregation import FleetThreadPage


def _get(path: str, view, **view_kwargs):
    factory = APIRequestFactory()
    request = factory.get(path)
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return view(request, **view_kwargs)


def test_thread_list_fleet_scope_serves_merged_page(monkeypatch):
    merged = [
        {"thread_id": "a", "updated_at": "2026-09-12T10:00:00+00:00"},
        {
            "thread_id": "old",
            "updated_at": "2026-06-01T00:00:00+00:00",
            "origin_device": "mini",
        },
    ]
    monkeypatch.setattr(
        thread_views,
        "fleet_thread_page",
        lambda **kwargs: FleetThreadPage(threads=list(merged), next_cursor="CUR"),
    )
    monkeypatch.setattr(thread_views, "get_session_manager", lambda: object())
    monkeypatch.setattr(thread_views, "get_livekit_shared_thread_id", lambda: None)
    monkeypatch.setattr(
        thread_views, "_refresh_projects_from_threads", lambda dirs: None
    )

    response = _get("/api/threads/?scope=fleet&page_size=2", thread_views.thread_list)

    assert response.status_code == 200
    assert [t["thread_id"] for t in response.data["threads"]] == ["a", "old"]
    assert response.data["threads"][1]["origin_device"] == "mini"
    assert "cursor=CUR" in response.data["next"]
    assert "scope=fleet" in response.data["next"]


def test_thread_list_without_scope_never_touches_fleet(monkeypatch):
    def explode(**kwargs):
        raise AssertionError("fleet path must not run without scope=fleet")

    monkeypatch.setattr(thread_views, "fleet_thread_page", explode)

    class EmptyManager:
        async def list_thread_page(self, *, limit, cursor=None):
            from openbase_coder_cli.thread_sync.session_manager import ThreadListPage

            return ThreadListPage(threads=[], next_cursor=None)

    monkeypatch.setattr(thread_views, "get_session_manager", EmptyManager)
    monkeypatch.setattr(thread_views, "get_livekit_shared_thread_id", lambda: None)

    response = _get("/api/threads/", thread_views.thread_list)

    assert response.status_code == 200
    assert response.data["threads"] == []


def test_all_reports_fleet_scope_merges(monkeypatch):
    monkeypatch.setattr(report_views, "_all_reports_items", lambda: [{"id": "local"}])
    monkeypatch.setattr(
        report_views,
        "fleet_report_items",
        lambda items: [*items, {"id": "peer", "origin_device": "mini"}],
    )

    plain = _get("/api/projects/reports/all/", report_views.all_project_reports)
    fleet = _get(
        "/api/projects/reports/all/?scope=fleet", report_views.all_project_reports
    )

    assert [i["id"] for i in plain.data["items"]] == ["local"]
    assert [i["id"] for i in fleet.data["items"]] == ["local", "peer"]


def test_report_file_device_param_proxies_get_and_rejects_writes(monkeypatch):
    from openbase_coder_cli.services.fleet_aggregation import PeerReportResponse

    captured = {}

    def fake_proxy(device, path, params, **kwargs):
        captured["device"] = device
        captured["path"] = path
        captured["params"] = params
        return PeerReportResponse(
            status_code=200,
            payload={"content": "hello"},
            content=None,
            content_type=None,
            filename=None,
        )

    monkeypatch.setattr(report_views, "proxy_peer_report_request", fake_proxy)

    response = _get(
        "/api/projects/reports/file/?path=/home/g/p&file=r.md&device=mini",
        report_views.project_reports_file,
    )
    assert response.status_code == 200
    assert response.data == {"content": "hello"}
    assert captured["device"] == "mini"
    assert captured["params"] == {"path": "/home/g/p", "file": "r.md"}

    factory = APIRequestFactory()
    request = factory.delete("/api/projects/reports/file/?path=/x&file=y&device=mini")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = report_views.project_reports_file(request)
    assert response.status_code == 400
