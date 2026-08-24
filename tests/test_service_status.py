from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    services_views,
    views,
)


def test_service_status_includes_background_openbase_services(monkeypatch) -> None:
    monkeypatch.setattr(
        services_views, "service_supports_configured_backends", lambda service: True
    )
    monkeypatch.setattr(services_views, "_check_port", lambda port: True)
    monkeypatch.setattr(services_views, "_check_tailscale", lambda: True)
    monkeypatch.setattr(services_views, "_check_web_backend", lambda: True)
    monkeypatch.setattr(
        services_views,
        "keep_awake_status_payload",
        lambda: {
            "name": "Keep Awake",
            "port": None,
            "running": True,
            "optional": False,
            "enabled": True,
            "command": "caffeinate -i -d",
            "assertions": [
                {"flag": "-i", "label": "Prevent idle sleep"},
                {"flag": "-d", "label": "Prevent display sleep"},
            ],
        },
    )
    monkeypatch.setattr(
        services_views,
        "tailscale_serve_health",
        lambda: SimpleNamespace(
            healthy=True,
            host="mac.tailnet.ts.net",
            openbase_url="http://mac.tailnet.ts.net:18080",
            openbase_configured=True,
            livekit_configured=True,
            openbase_reachable=True,
            error=None,
        ),
    )

    def fake_launchctl_status(service):
        return {
            "installed": True,
            "pid": "123" if service.name == "sync-workers" else None,
            "last_exit_code": None,
        }

    monkeypatch.setattr(services_views, "launchctl_status", fake_launchctl_status)

    request = APIRequestFactory().get("/api/status/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.service_status(request)

    assert response.status_code == 200
    assert response.data["services"]["sync_workers"] == {
        "name": "Sync Workers (thread, device, and code-sync reconcile)",
        "port": None,
        "running": True,
        "installed": True,
        "last_exit_code": None,
        "optional": False,
    }
    assert response.data["services"]["openbase_routines"] == {
        "name": "Openbase Routines",
        "port": None,
        "running": False,
        "installed": True,
        "last_exit_code": None,
        "optional": False,
    }
    assert response.data["services"]["tailscale_serve"] == {
        "name": "Tailscale Serve",
        "port": 18080,
        "running": True,
        "host": "mac.tailnet.ts.net",
        "url": "http://mac.tailnet.ts.net:18080",
        "openbase_configured": True,
        "livekit_configured": True,
        "openbase_reachable": True,
        "error": None,
        "optional": False,
    }
    assert response.data["services"]["keep_awake"] == {
        "name": "Keep Awake",
        "port": None,
        "running": True,
        "optional": False,
        "enabled": True,
        "command": "caffeinate -i -d",
        "assertions": [
            {"flag": "-i", "label": "Prevent idle sleep"},
            {"flag": "-d", "label": "Prevent display sleep"},
        ],
    }
    assert len(response.data["services"]) == 10


def test_service_status_omits_codex_app_server_on_claude_code_backend(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        services_views,
        "service_supports_configured_backends",
        lambda service: service.supports_backend("claude_code"),
    )
    monkeypatch.setattr(services_views, "_check_port", lambda port: True)
    monkeypatch.setattr(services_views, "_check_tailscale", lambda: True)
    monkeypatch.setattr(services_views, "_check_web_backend", lambda: True)
    monkeypatch.setattr(
        services_views,
        "keep_awake_status_payload",
        lambda: {
            "name": "Keep Awake",
            "port": None,
            "running": True,
            "optional": False,
            "enabled": True,
            "command": "caffeinate -i -d",
            "assertions": [],
        },
    )
    monkeypatch.setattr(
        services_views,
        "tailscale_serve_health",
        lambda: SimpleNamespace(
            healthy=True,
            host="mac.tailnet.ts.net",
            openbase_url="http://mac.tailnet.ts.net:18080",
            openbase_configured=True,
            livekit_configured=True,
            openbase_reachable=True,
            error=None,
        ),
    )
    monkeypatch.setattr(
        services_views,
        "launchctl_status",
        lambda service: {"installed": True, "pid": "123", "last_exit_code": None},
    )

    request = APIRequestFactory().get("/api/status/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.service_status(request)

    assert response.status_code == 200
    assert "codex_app_server" not in response.data["services"]
    assert "sync_workers" in response.data["services"]
    assert len(response.data["services"]) == 9


def test_thread_device_sync_status_returns_snapshot_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        services_views,
        "thread_snapshot_status",
        lambda: {
            "device": {"device_id": "device-1", "device_name": "Laptop"},
            "exchange_dir": "/tmp/exchange",
            "ledger_path": "/tmp/ledger.json",
            "snapshot_count": 2,
            "thread_count": 1,
            "conflict_count": 1,
            "conflicts": [{"thread_id": "thread-1", "reason": "divergent_fingerprint"}],
        },
    )
    monkeypatch.setattr(
        services_views,
        "claude_thread_snapshot_status",
        lambda: {
            "device": {"device_id": "device-1", "device_name": "Laptop"},
            "exchange_dir": "/tmp/exchange",
            "ledger_path": "/tmp/claude-ledger.json",
            "snapshot_count": 1,
            "session_count": 1,
            "conflict_count": 1,
            "conflicts": [
                {"session_id": "session-1", "reason": "divergent_fingerprint"}
            ],
        },
    )

    request = APIRequestFactory().get("/api/settings/thread-device-sync/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = views.thread_device_sync_status(request)

    assert response.status_code == 200
    # Existing top-level codex fields are unchanged.
    assert response.data["device"]["device_id"] == "device-1"
    assert response.data["snapshot_count"] == 2
    assert response.data["thread_count"] == 1
    assert response.data["conflict_count"] == 1
    assert response.data["conflicts"][0]["backend"] == "codex"
    # Claude arrives as a sibling object with the same shape.
    assert response.data["claude"]["session_count"] == 1
    assert response.data["claude"]["ledger_path"] == "/tmp/claude-ledger.json"
    assert response.data["claude"]["conflicts"][0]["backend"] == "claude"


def test_thread_device_sync_conflicts_includes_both_backends(monkeypatch) -> None:
    monkeypatch.setattr(
        services_views,
        "thread_snapshot_conflicts_payload",
        lambda: {
            "device": {"device_id": "device-1"},
            "exchange_dir": "/tmp/exchange",
            "ledger_path": "/tmp/ledger.json",
            "conflict_count": 1,
            "conflicts": [{"thread_id": "thread-1", "source_type": "device"}],
        },
    )
    monkeypatch.setattr(
        services_views,
        "claude_thread_snapshot_conflicts_payload",
        lambda: {
            "device": {"device_id": "device-1"},
            "exchange_dir": "/tmp/exchange",
            "ledger_path": "/tmp/claude-ledger.json",
            "conflict_count": 1,
            "conflicts": [{"session_id": "session-1", "source_type": "device"}],
        },
    )

    request = APIRequestFactory().get("/api/settings/thread-device-sync/conflicts/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = services_views.thread_device_sync_conflicts(request)

    assert response.status_code == 200
    assert response.data["conflict_count"] == 1
    assert response.data["conflicts"] == [
        {"thread_id": "thread-1", "source_type": "device", "backend": "codex"}
    ]
    assert response.data["claude"]["conflicts"] == [
        {"session_id": "session-1", "source_type": "device", "backend": "claude"}
    ]


def test_thread_sync_conflicts_returns_aggregate_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        services_views,
        "thread_sync_conflicts_payload",
        lambda: {
            "conflict_count": 2,
            "home_conflict_count": 1,
            "device_conflict_count": 1,
            "conflicts": [
                {"source_type": "home", "thread_id": "thread-home"},
                {"source_type": "device", "thread_id": "thread-device"},
            ],
        },
    )
    monkeypatch.setattr(
        services_views,
        "claude_thread_sync_conflicts_payload",
        lambda: {
            "conflict_count": 1,
            "home_conflict_count": 0,
            "device_conflict_count": 1,
            "conflicts": [
                {"source_type": "device", "session_id": "session-device"},
            ],
        },
    )

    request = APIRequestFactory().get("/api/settings/thread-sync/conflicts/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = services_views.thread_sync_conflicts(request)

    assert response.status_code == 200
    assert response.data["conflict_count"] == 2
    assert {item["source_type"] for item in response.data["conflicts"]} == {
        "home",
        "device",
    }
    assert all(item["backend"] == "codex" for item in response.data["conflicts"])
    assert response.data["claude"]["conflict_count"] == 1
    assert response.data["claude"]["conflicts"][0]["backend"] == "claude"


def test_thread_device_sync_conflict_resolve_defaults_to_codex(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_codex_resolve(thread_id, *, action):
        calls.append((thread_id, action))
        return {"thread_id": thread_id, "action": action}

    monkeypatch.setattr(
        services_views, "resolve_thread_snapshot_conflict", fake_codex_resolve
    )

    request = APIRequestFactory().post(
        "/api/settings/thread-device-sync/conflicts/thread-1/resolve/",
        {"action": "accept_local"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = services_views.thread_device_sync_conflict_resolve(request, "thread-1")

    assert response.status_code == 200
    assert calls == [("thread-1", "accept_local")]


def test_thread_device_sync_conflict_resolve_dispatches_to_claude(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_claude_resolve(session_id, *, action):
        calls.append((session_id, action))
        return {"session_id": session_id, "action": action}

    monkeypatch.setattr(
        services_views, "resolve_claude_snapshot_conflict", fake_claude_resolve
    )

    request = APIRequestFactory().post(
        "/api/settings/thread-device-sync/conflicts/session-1/resolve/",
        {"action": "accept_remote_latest", "backend": "claude"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = services_views.thread_device_sync_conflict_resolve(request, "session-1")

    assert response.status_code == 200
    assert calls == [("session-1", "accept_remote_latest")]
    assert response.data["session_id"] == "session-1"


def test_thread_device_sync_conflict_resolve_reports_claude_errors(
    monkeypatch,
) -> None:
    from openbase_coder_cli.thread_sync.claude_thread_sync import (
        ClaudeConflictResolutionError,
    )

    def fail_resolve(session_id, *, action):
        raise ClaudeConflictResolutionError("conflict_not_found")

    monkeypatch.setattr(
        services_views, "resolve_claude_snapshot_conflict", fail_resolve
    )

    request = APIRequestFactory().post(
        "/api/settings/thread-device-sync/conflicts/session-1/resolve/",
        {"action": "accept_local", "backend": "claude"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))

    response = services_views.thread_device_sync_conflict_resolve(request, "session-1")

    assert response.status_code == 400
    assert response.data["error"] == "conflict_not_found"
