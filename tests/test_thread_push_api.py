"""Thread push API views, CLI command, payload annotation and fleet merge."""

from __future__ import annotations

# ruff: noqa: E402, I001

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django
import pytest
from click.testing import CliRunner
from rest_framework.test import APIRequestFactory, force_authenticate

django.setup()

import importlib
from openbase_coder_cli.openbase_coder_cli_app import thread_push_api as views
from openbase_coder_cli.openbase_coder_cli_app.thread_metadata import (
    annotate_thread_payload,
)
from openbase_coder_cli.services import fleet_aggregation as fleet
from openbase_coder_cli.services import thread_push
from openbase_coder_cli.services.thread_push import PushError
from openbase_coder_cli.thread_sync import thread_moves

USER = SimpleNamespace(is_authenticated=True)
# ``openbase_coder_cli.cli.threads`` is shadowed by the click group of that name.
threads_cli = importlib.import_module("openbase_coder_cli.cli.threads")


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(views, "get_session_manager", lambda: object())
    monkeypatch.setattr(views, "invalidate_thread_list_cache", lambda: None)


def _post(view, path: str, data: dict, *args):
    request = APIRequestFactory().post(path, data, format="json")
    force_authenticate(request, user=USER)
    return view(request, *args)


def _get(view, path: str, *args):
    request = APIRequestFactory().get(path)
    force_authenticate(request, user=USER)
    return view(request, *args)


def test_push_view_passes_options_and_returns_result(monkeypatch) -> None:
    seen = {}

    async def push(manager, thread_id, **kwargs):
        seen.update(kwargs, thread_id=thread_id)
        return {"state": "moved", "thread_id": thread_id}

    monkeypatch.setattr(thread_push, "push_thread", push)
    request_id = "00000000-0000-4000-8000-0000000000aa"

    response = _post(
        views.thread_push,
        "/api/threads/t-1/push/",
        {"to": "mini", "message": "go on", "request_id": request_id},
        "t-1",
    )

    assert response.status_code == 200
    assert response.data["state"] == "moved"
    assert seen == {
        "thread_id": "t-1",
        "to": "mini",
        "message": "go on",
        "request_id": request_id,
    }


def test_push_view_maps_errors_with_retry_hint(monkeypatch) -> None:
    async def push(manager, thread_id, **kwargs):
        raise PushError("thread_busy", "A turn is running.", safe_to_retry=True)

    monkeypatch.setattr(thread_push, "push_thread", push)

    response = _post(views.thread_push, "/api/threads/t-1/push/", {}, "t-1")

    assert response.status_code == 409
    assert response.data == {
        "error": "A turn is running.",
        "code": "thread_busy",
        "safe_to_retry": True,
    }


def test_push_view_rejects_unknown_fields() -> None:
    response = _post(views.thread_push, "/api/threads/t-1/push/", {"bogus": 1}, "t-1")
    assert response.status_code == 400


def test_target_and_arrival_views(monkeypatch) -> None:
    monkeypatch.setattr(
        thread_push, "target_capabilities", lambda: {"accepts_pushes": True}
    )
    assert _get(views.thread_push_target, "/x/").data == {"accepts_pushes": True}

    async def accept(manager, payload):
        return {"state": "ready", "thread_id": payload["thread_id"]}

    monkeypatch.setattr(thread_push, "accept_push", accept)
    response = _post(views.thread_push_arrivals, "/x/", {"thread_id": "t-1"})
    assert response.data["thread_id"] == "t-1"

    missing = _get(
        views.thread_push_arrival_detail,
        "/x/",
        "00000000-0000-4000-8000-0000000000bb",
    )
    assert missing.status_code == 404


def test_release_view_clears_the_marker() -> None:
    thread_moves.set_move("t-1", state=thread_moves.STATE_MOVED, target={"name": "m"})

    response = _post(views.thread_push_release, "/x/", {}, "t-1")

    assert response.data["released"] is True
    assert thread_moves.get_move("t-1") is None


def test_annotated_payload_carries_moved_to() -> None:
    thread_moves.set_move(
        "t-1",
        state=thread_moves.STATE_MOVED,
        target={"name": "mini", "host": "mini.ts.net"},
        target_thread_id="t-1",
        moved_at="2026-10-08T12:00:00+00:00",
    )

    payload = annotate_thread_payload({"thread_id": "t-1", "directory": "/tmp"})

    assert payload["moved_to"] == {
        "state": "moved",
        "device": "mini",
        "host": "mini.ts.net",
        "thread_id": "t-1",
        "at": "2026-10-08T12:00:00+00:00",
    }
    assert annotate_thread_payload({"thread_id": "t-2"})["moved_to"] is None


def test_failed_push_does_not_mark_the_payload() -> None:
    thread_moves.set_move("t-1", state=thread_moves.STATE_FAILED, target={"name": "m"})
    assert thread_moves.moved_to_payload("t-1") is None
    thread_moves.ensure_thread_writable("t-1")


def test_fleet_list_prefers_the_durable_copy_of_a_moved_thread(monkeypatch) -> None:
    peer = fleet.FleetPeer(key="mini.ts.net", name="mini", base_url="http://mini")
    monkeypatch.setattr(fleet, "owner_access_token", lambda: "token")
    monkeypatch.setattr(fleet, "fleet_peers", lambda: [peer])
    local = {
        "thread_id": "t-1",
        "updated_at": "2026-10-08T12:00:00+00:00",
        "moved_to": {"state": "moved", "host": "mini.ts.net"},
    }
    remote = {
        "thread_id": "t-1",
        "updated_at": "2026-10-08T11:00:00+00:00",
        fleet.ORIGIN_DEVICE_KEY: "mini",
        fleet.ORIGIN_HOST_KEY: "mini.ts.net",
    }
    monkeypatch.setattr(
        fleet,
        "_fetch_peer_thread_page",
        lambda *a, **k: fleet.SourcePage(items=[dict(remote)], next_cursor=None),
    )

    page = fleet.fleet_thread_page(
        page_size=10,
        cursor=None,
        fetch_local_page=lambda *a: fleet.SourcePage(items=[local], next_cursor=None),
    )

    assert [item.get(fleet.ORIGIN_DEVICE_KEY) for item in page.threads] == ["mini"]


def test_fleet_list_keeps_the_local_copy_while_the_target_is_offline(
    monkeypatch,
) -> None:
    monkeypatch.setattr(fleet, "owner_access_token", lambda: "token")
    monkeypatch.setattr(fleet, "fleet_peers", lambda: [])
    local = {
        "thread_id": "t-1",
        "updated_at": "2026-10-08T12:00:00+00:00",
        "moved_to": {"state": "moved", "host": "mini.ts.net"},
    }

    page = fleet.fleet_thread_page(
        page_size=10,
        cursor=None,
        fetch_local_page=lambda *a: fleet.SourcePage(items=[local], next_cursor=None),
    )

    assert [item["thread_id"] for item in page.threads] == ["t-1"]


def test_cli_push_reports_the_move(monkeypatch) -> None:
    calls = []

    def api(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        return 200, {
            "state": "moved",
            "moved_to": {"device": "mini", "thread_id": "t-1"},
            "turn_started": True,
        }

    monkeypatch.setattr(threads_cli, "_push_api", api)

    result = CliRunner().invoke(
        threads_cli.threads, ["push", "t-1", "--to", "mini", "-m", "go on"]
    )

    assert result.exit_code == 0, result.output
    assert "Pushed to mini" in result.output
    assert "message was sent" in result.output
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/api/threads/t-1/push/")
    assert body["to"] == "mini" and body["message"] == "go on"
    assert body["request_id"]


def test_cli_push_waits_for_a_running_turn(monkeypatch) -> None:
    answers = [
        (409, {"code": "thread_busy", "error": "A turn is running."}),
        (200, {"state": "moved", "moved_to": {"device": "mini", "thread_id": "t"}}),
    ]
    bodies = []

    def api(method, path, **kwargs):
        bodies.append(kwargs.get("json"))
        return answers.pop(0)

    monkeypatch.setattr(threads_cli, "_push_api", api)
    monkeypatch.setattr(threads_cli, "PUSH_WAIT_POLL_SECONDS", 0)

    result = CliRunner().invoke(threads_cli.threads, ["push", "t", "--wait"])

    assert result.exit_code == 0, result.output
    # Every retry reuses the same request id, so the push stays idempotent.
    assert bodies[0]["request_id"] == bodies[1]["request_id"]


def test_cli_push_surfaces_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        threads_cli,
        "_push_api",
        lambda *a, **k: (
            409,
            {
                "error": "mini is offline.",
                "code": "target_offline",
                "safe_to_retry": True,
            },
        ),
    )

    result = CliRunner().invoke(threads_cli.threads, ["push", "t"])

    assert result.exit_code != 0
    assert "mini is offline. (safe to retry)" in result.output


def test_cli_push_list_and_flag_validation(monkeypatch) -> None:
    monkeypatch.setattr(
        threads_cli,
        "_push_api",
        lambda *a, **k: (
            200,
            {
                "blocked_reason": None,
                "targets": [
                    {
                        "name": "mini",
                        "key": "mini.ts.net",
                        "online": True,
                        "reason": None,
                    }
                ],
            },
        ),
    )
    listed = CliRunner().invoke(threads_cli.threads, ["push", "t", "--list"])
    assert "mini [mini.ts.net] online" in listed.output

    both = CliRunner().invoke(threads_cli.threads, ["push", "t", "--list", "--cancel"])
    assert both.exit_code != 0
    force = CliRunner().invoke(threads_cli.threads, ["push", "t", "--force"])
    assert force.exit_code != 0
