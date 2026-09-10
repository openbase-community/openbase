from __future__ import annotations

import asyncio
import base64
import importlib
import json
from pathlib import Path
from typing import Any

from super_agents.app_client_events import EventClientMixin
from super_agents.app_client_routines import RoutineClientMixin
from super_agents.state import read_state_file

cloud_webhook_events = importlib.import_module(
    "openbase_coder_cli.services.cloud_webhook_events"
)
cloud_registration = importlib.import_module(
    "openbase_coder_cli.services.cloud_registration"
)


class _Response:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class _FakeTokenManager:
    def __init__(self, backend_url: str) -> None:
        self.backend_url = backend_url

    def get_access_token(self) -> str:
        return "jwt-token"


def _mock_cloud(monkeypatch, responses: list[tuple[int, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    queue = list(responses)

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        status_code, payload = queue.pop(0)
        return _Response(status_code, payload)

    monkeypatch.setattr(cloud_registration, "TokenManager", _FakeTokenManager)
    monkeypatch.setattr(cloud_registration.httpx, "request", fake_request)
    return calls


def test_create_relay_endpoint_posts_payload(monkeypatch) -> None:
    calls = _mock_cloud(
        monkeypatch, [(201, {"id": "ep-1", "token": "obhk_x", "url": "https://x/t/"})]
    )

    result = cloud_webhook_events.create_relay_endpoint(description="PR comments")

    assert result.ok
    assert result.response["id"] == "ep-1"
    assert calls[0]["url"].endswith("/api/openbase/hooks/endpoints/")
    assert calls[0]["json"] == {"description": "PR comments"}
    assert calls[0]["headers"]["Authorization"] == "Bearer jwt-token"


def test_fetch_pending_degrades_when_endpoint_missing(monkeypatch) -> None:
    _mock_cloud(monkeypatch, [(404, {"detail": "nope"})])

    result = cloud_webhook_events.fetch_pending_relay_events()

    assert not result.ok
    assert not result.supported


class RelayClientStub(RoutineClientMixin, EventClientMixin):
    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file
        self._state_lock = asyncio.Lock()

    async def read_state(self):
        return read_state_file(self.state_file)


def test_deliver_relay_events_delivers_matches_and_acks(monkeypatch, tmp_path) -> None:
    client = RelayClientStub(tmp_path / "state.json")
    asyncio.run(
        client.save_routine(
            {
                "name": "echo-loop",
                "kind": "command",
                "command": "printenv SUPER_AGENTS_EVENT_JSON",
                "scheduleType": "interval",
                "intervalSeconds": 3600,
            }
        )
    )
    created = asyncio.run(
        client.add_routine_trigger("echo-loop", {"relayEndpointId": "ep-1"})
    )
    assert created["trigger"]["relayEndpointId"] == "ep-1"

    ack_calls = _mock_cloud(monkeypatch, [(200, {"acked": 1})])
    body = json.dumps({"action": "created"}).encode()
    events = [
        {
            "id": "evt-1",
            "endpointId": "ep-1",
            "headers": {"X-GitHub-Delivery": "guid-9"},
            "bodyBase64": base64.b64encode(body).decode(),
        },
        {
            "id": "evt-other-device",
            "endpointId": "ep-unknown",
            "headers": {},
            "bodyBase64": base64.b64encode(b"{}").decode(),
        },
    ]

    summary = asyncio.run(cloud_webhook_events.deliver_relay_events(client, events))

    assert summary == {"fetched": 2, "matched": 1, "delivered": 1, "acked": 1}
    assert ack_calls[0]["url"].endswith("/api/openbase/hooks/events/ack/")
    assert ack_calls[0]["json"] == {"ids": ["evt-1"]}
    routine = read_state_file(tmp_path / "state.json").routines["echo-loop"]
    assert routine.last_status == "completed"


def test_sync_workers_tick_skips_without_login(monkeypatch, caplog) -> None:
    sync_workers = importlib.import_module("openbase_coder_cli.cli.sync_workers")
    token_manager = importlib.import_module("openbase_coder_cli.config.token_manager")

    class _LoggedOut:
        def __init__(self, backend_url: str) -> None:
            pass

        @property
        def has_refresh_token(self) -> bool:
            return False

    monkeypatch.setattr(token_manager, "TokenManager", _LoggedOut)

    with caplog.at_level("DEBUG"):
        sync_workers._cloud_webhook_events_tick()

    assert any(
        "cloud_webhook_events skipped no_login" in r.message for r in caplog.records
    )


def test_sync_workers_job_registered() -> None:
    sync_workers = importlib.import_module("openbase_coder_cli.cli.sync_workers")
    names = [job.name for job in sync_workers.build_jobs()]
    assert "cloud_webhook_events" in names
