from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import lockdown  # noqa: E402


class FakeManager:
    async def list_approval_requests(self):
        return [
            {
                "id": "request-1",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "backend": "codex",
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "toolCallId": "tool-1",
                    "toolName": "shell",
                    "command": "true",
                    "description": "Run a command",
                },
            }
        ]


class FakeBroker:
    def __init__(self):
        self.scope = None

    def status(self):
        return {"enabled": True, "health": "ready", "challenge": None}

    def create_challenge(self, scope, *, action_summary):
        self.scope = scope
        return {
            "id": "challenge-1",
            "state": "awaiting_phrase",
            "requestId": scope.request_id,
            "actionSummary": action_summary,
        }


def _authenticated(request):
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def test_status_requires_authentication():
    response = lockdown.lockdown_status(APIRequestFactory().get("/api/lockdown/"))
    assert response.status_code in {401, 403}


def test_challenge_api_uses_pending_request_and_returns_no_secret(monkeypatch):
    broker = FakeBroker()
    monkeypatch.setattr(lockdown, "get_session_manager", lambda: FakeManager())
    monkeypatch.setattr(lockdown, "get_voice_lockdown_broker", lambda: broker)
    request = _authenticated(
        APIRequestFactory().post(
            "/api/lockdown/challenges/",
            {
                "requestId": "request-1",
                "roomSid": "room-1",
                "participantIdentity": "owner-1",
            },
            format="json",
        )
    )
    response = lockdown.lockdown_challenges(request)
    assert response.status_code == 201
    assert broker.scope.request_id == "request-1"
    assert broker.scope.room_sid == "room-1"
    serialized = str(response.data).lower()
    for forbidden in ("'phrase':", "verifier", "capability", "salt", "pepper"):
        assert forbidden not in serialized
