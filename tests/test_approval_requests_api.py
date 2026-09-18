from __future__ import annotations

import json
import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from django.test import Client  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import approvals  # noqa: E402
from openbase_coder_cli.skill_approvals import APPROVAL_METHOD  # noqa: E402


class FakeApprovalManager:
    def __init__(self, requests: list[dict]) -> None:
        self.requests = requests
        self.answered: list[tuple[str, str]] = []

    async def list_approval_requests(self) -> list[dict]:
        return self.requests

    async def answer_approval_request(self, request_id: str, decision: str) -> dict:
        self.answered.append((request_id, decision))
        return {"requestId": request_id, "result": {"decision": decision}}


def _factory_get():
    request = APIRequestFactory().get("/api/approval-requests/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def test_approval_requests_keeps_codex_and_pending_skill_requests(monkeypatch):
    codex_request = {
        "id": "codex-1",
        "method": "mcpServer/elicitation/request",
        "params": {"serverName": "filesystem", "description": "Codex needs input"},
        "received_at": "2026-01-01T00:00:00Z",
    }
    pending_skill_request = {
        "id": "skill-1",
        "method": APPROVAL_METHOD,
        "params": {"source": "skill", "description": "Skill needs approval"},
        "received_at": "2026-01-01T00:00:01Z",
    }
    answered_skill_request = {
        "id": "skill-2",
        "method": APPROVAL_METHOD,
        "params": {"source": "skill", "description": "Already answered"},
        "received_at": "2026-01-01T00:00:02Z",
    }

    monkeypatch.setattr(
        approvals,
        "get_session_manager",
        lambda: FakeApprovalManager(
            [codex_request, pending_skill_request, answered_skill_request]
        ),
    )
    monkeypatch.setattr(
        approvals,
        "is_pending_skill_approval_request",
        lambda request: request["id"] == "skill-1",
    )

    response = approvals.approval_requests(_factory_get())

    assert response.status_code == 200
    assert response.data["requests"] == [codex_request, pending_skill_request]


def test_answer_request_falls_back_to_codex_manager(monkeypatch):
    manager = FakeApprovalManager([])
    monkeypatch.setattr(approvals, "get_session_manager", lambda: manager)
    monkeypatch.setattr(
        approvals,
        "answer_skill_approval_request",
        lambda request_id, decision: (_ for _ in ()).throw(ValueError("not skill")),
    )

    request = APIRequestFactory().post(
        "/api/approval-requests/codex-1/",
        {"decision": "accept"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = approvals.approval_request_detail(request, "codex-1")

    assert response.status_code == 200
    assert manager.answered == [("codex-1", "accept")]


def test_skill_approval_request_api_allows_authenticated_local_post_without_csrf_token(
    monkeypatch,
):
    captured = {}

    def fake_authenticate(self, request):
        return (SimpleNamespace(is_authenticated=True), {"sub": "user-1"})

    def fake_create_skill_approval_request(**kwargs):
        captured.update(kwargs)
        return {
            "id": "skill-approval-1",
            "method": APPROVAL_METHOD,
            "params": {
                "source": "skill",
                "skill": kwargs["skill"],
                "action": kwargs["action"],
                "description": kwargs["description"],
            },
            "received_at": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr(
        "openbase_coder_cli.config.authentication.JWTAuthentication.authenticate",
        fake_authenticate,
    )
    monkeypatch.setattr(
        approvals,
        "create_skill_approval_request",
        fake_create_skill_approval_request,
    )

    client = Client(enforce_csrf_checks=True)
    response = client.post(
        "/api/skill-approval-requests/",
        data=json.dumps(
            {
                "skill": "whatsapp-cli",
                "action": "approve-contact",
                "description": "Approve WhatsApp contact",
                "details": {"contact_id": "207829222858962@lid"},
                "timeout_seconds": 300,
            }
        ),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer jwt.token.value",
        HTTP_HOST="localhost",
    )

    assert response.status_code == 201
    assert response.json()["request"]["id"] == "skill-approval-1"
    assert captured["skill"] == "whatsapp-cli"
    assert captured["details"] == {"contact_id": "207829222858962@lid"}


def test_remote_approval_never_answers_same_id_locally(monkeypatch):
    from unittest.mock import Mock

    local_answer = Mock()
    peer_answer = Mock(return_value=(200, {"success": True}))
    manager = FakeApprovalManager([])
    monkeypatch.setattr(approvals, "answer_skill_approval_request", local_answer)
    monkeypatch.setattr(approvals, "get_session_manager", lambda: manager)
    monkeypatch.setattr(approvals, "answer_peer_approval", peer_answer)
    request = APIRequestFactory().post(
        "/api/approval-requests/shared-id/",
        {"decision": "accept", "origin_host": "mini.example"},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = approvals.approval_request_detail(request, "shared-id")
    assert response.status_code == 200
    peer_answer.assert_called_once_with("mini.example", "shared-id", "accept")
    local_answer.assert_not_called()
    assert manager.answered == []


def test_remote_approval_requires_local_authentication(monkeypatch):
    from unittest.mock import Mock

    peer_answer = Mock()
    monkeypatch.setattr(approvals, "answer_peer_approval", peer_answer)
    request = APIRequestFactory().post(
        "/api/approval-requests/id/",
        {"decision": "accept", "origin_host": "mini.example"},
        format="json",
    )
    response = approvals.approval_request_detail(request, "id")
    assert response.status_code in (401, 403)
    peer_answer.assert_not_called()
