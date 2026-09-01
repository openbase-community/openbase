from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli import inbound_calls  # noqa: E402
from openbase_coder_cli.config import cloud_inbound_calls  # noqa: E402
from openbase_coder_cli.config.cloud_inbound_calls import (  # noqa: E402
    CloudInboundCallAcceptance,
)
from openbase_coder_cli.livekit_voice_history import VoiceHistoryEntry  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import inbound_calls as api  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import livekit  # noqa: E402

INVITATION_ID = "A" * 43


@pytest.fixture()
def invitation_state(monkeypatch, tmp_path: Path) -> Path:
    state_path = tmp_path / "private" / "inbound-call-invitations.json"
    monkeypatch.setattr(inbound_calls, "STATE_PATH", state_path)
    return state_path


def _create(*, account: str = "gabe@example.com", now: float = 100.0):
    return inbound_calls.create_invitation(
        invitation_id=INVITATION_ID,
        account_identity=account,
        caller_name="Dottie",
        thread_id="thread-1",
        cwd="/repo",
        agent_name="Dottie",
        livekit_dispatch_agent_name="livekit-agent",
        now=now,
    )


def _request(path: str, data: dict, *, account: str = "gabe@example.com"):
    request = APIRequestFactory().post(
        path,
        data=data,
        format="json",
        HTTP_AUTHORIZATION="Bearer jwt.token.value",
    )
    user = SimpleNamespace(
        is_authenticated=True,
        email=account,
        pk=1,
        get_full_name=lambda: "Gabe",
    )
    force_authenticate(request, user=user, token={"email": account})
    return request


def test_invitation_state_is_owner_only_atomic_and_account_scoped(
    invitation_state: Path,
) -> None:
    created = _create()

    assert created.room_name.startswith("room-inbound-")
    assert invitation_state.stat().st_mode & 0o777 == 0o600
    assert invitation_state.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(inbound_calls.InboundCallInvitationError):
        inbound_calls.answer_invitation(
            INVITATION_ID,
            account_identity="other@example.com",
            now=101,
        )
    answered = inbound_calls.answer_invitation(
        INVITATION_ID,
        account_identity="gabe@example.com",
        now=101,
    )
    assert answered.status == "answered"
    assert (
        inbound_calls.answer_invitation(
            INVITATION_ID,
            account_identity="gabe@example.com",
            now=102,
        ).status
        == "answered"
    )


def test_invitation_rejects_collision_and_expiry(invitation_state: Path) -> None:
    _create()
    with pytest.raises(inbound_calls.InboundCallInvitationConflict):
        _create(now=101)
    with pytest.raises(inbound_calls.InboundCallInvitationExpired):
        inbound_calls.answer_invitation(
            INVITATION_ID,
            account_identity="gabe@example.com",
            now=161,
        )


def test_invitation_state_ignores_symlink_and_malformed_entries(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"schema_version": 1, "invitations": {}}))
    state_path = tmp_path / "state.json"
    state_path.symlink_to(target)
    monkeypatch.setattr(inbound_calls, "STATE_PATH", state_path)

    with pytest.raises(inbound_calls.InboundCallInvitationError):
        inbound_calls.answer_invitation(
            INVITATION_ID,
            account_identity="gabe@example.com",
            now=100,
        )


def test_cloud_ring_uses_only_opaque_id_and_caller(monkeypatch) -> None:
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            202,
            json={
                "invitation_id": INVITATION_ID,
                "expires_at": 160,
                "device_count": 2,
            },
        )

    monkeypatch.setattr(cloud_inbound_calls, "web_backend_url", lambda: "https://cloud")
    monkeypatch.setattr(cloud_inbound_calls.httpx, "post", fake_post)

    result = cloud_inbound_calls.request_inbound_call_ring(
        invitation_id=INVITATION_ID,
        caller_name="Dottie",
        access_token="jwt.token.value",
    )

    assert result.device_count == 2
    assert calls[0][0] == "https://cloud/api/openbase/voice/invitations/"
    assert calls[0][1]["json"] == {
        "invitation_id": INVITATION_ID,
        "caller_name": "Dottie",
    }


def test_user_call_persists_before_cloud_and_sets_server_expiry(
    monkeypatch,
    invitation_state: Path,
) -> None:
    monkeypatch.setattr(api, "new_invitation_id", lambda: INVITATION_ID)
    monkeypatch.setattr(
        api,
        "get_voice_history_entry_for_agent_name",
        lambda _name: VoiceHistoryEntry(
            thread_id="thread-1",
            agent_name="Dottie",
            cwd="/repo",
            voice_id=None,
            voice_name=None,
            kind="agent",
            source="test",
            first_seen_at=1,
            last_seen_at=2,
        ),
    )

    def ring(**kwargs):
        payload = json.loads(invitation_state.read_text(encoding="utf-8"))
        assert INVITATION_ID in payload["invitations"]
        assert kwargs == {
            "invitation_id": INVITATION_ID,
            "caller_name": "Dottie",
            "access_token": "jwt.token.value",
        }
        return CloudInboundCallAcceptance(INVITATION_ID, int(time.time()) + 55, 2)

    monkeypatch.setattr(api, "request_inbound_call_ring", ring)
    response = api.user_call(_request("/api/user/call/", {"agent_name": "Dottie"}))

    assert response.status_code == 202
    assert response.data["device_count"] == 2
    assert response.data["invitation_id"] == INVITATION_ID
    stored = json.loads(invitation_state.read_text(encoding="utf-8"))
    assert stored["invitations"][INVITATION_ID]["expires_at"] == response.data["expires_at"]


def test_user_call_rolls_back_pending_state_when_cloud_fails(
    monkeypatch,
    invitation_state: Path,
) -> None:
    monkeypatch.setattr(api, "new_invitation_id", lambda: INVITATION_ID)
    monkeypatch.setattr(
        api,
        "get_voice_history_entry_for_agent_name",
        lambda _name: SimpleNamespace(
            thread_id="thread-1", cwd="/repo", agent_name="Dottie"
        ),
    )
    monkeypatch.setattr(
        api,
        "request_inbound_call_ring",
        lambda **_kwargs: (_ for _ in ()).throw(
            cloud_inbound_calls.InboundCallCloudError("No devices.")
        ),
    )

    response = api.user_call(_request("/api/user/call/", {"agent_name": "Dottie"}))

    assert response.status_code == 409
    payload = json.loads(invitation_state.read_text(encoding="utf-8"))
    assert INVITATION_ID not in payload["invitations"]


def test_inbound_token_uses_stored_route_without_exposing_it(
    monkeypatch,
    invitation_state: Path,
) -> None:
    _create(now=time.time())
    monkeypatch.setattr(
        livekit, "_livekit_client_token_credentials", lambda: ("key", "secret")
    )
    monkeypatch.setattr(
        livekit,
        "local_audio_readiness",
        lambda **_kwargs: SimpleNamespace(ready=True, detail=None),
    )
    monkeypatch.setattr(
        livekit, "ensure_openbase_cloud_audio_subscription", lambda **_kwargs: None
    )
    monkeypatch.setattr(livekit, "cloud_workspace_id", lambda: None)

    response = livekit.livekit_room_token(
        _request(
            "/api/livekit-room-token/",
            {"inbound_invitation_id": INVITATION_ID},
        )
    )

    assert response.status_code == 200
    assert response.data["room_name"].startswith("room-inbound-")
    assert response.data["requires_route_activation"] is True
    assert response.data["inbound_invitation_id"] == INVITATION_ID
    assert "thread_id" not in response.data
    assert "cwd" not in response.data


def test_activation_requires_answer_and_marks_only_after_publish(
    monkeypatch,
    invitation_state: Path,
) -> None:
    invitation = _create(now=time.time())
    response = api.inbound_call_activate(
        _request(
            "/api/inbound-call/activate/",
            {
                "inbound_invitation_id": INVITATION_ID,
                "room_name": invitation.room_name,
            },
        )
    )
    assert response.status_code == 409

    inbound_calls.answer_invitation(
        INVITATION_ID, account_identity="gabe@example.com"
    )
    calls = []

    async def publish(thread_id, **kwargs):
        calls.append((thread_id, kwargs))

    monkeypatch.setattr(api, "publish_transfer_to_thread", publish)
    response = api.inbound_call_activate(
        _request(
            "/api/inbound-call/activate/",
            {
                "inbound_invitation_id": INVITATION_ID,
                "room_name": invitation.room_name,
            },
        )
    )
    assert response.status_code == 200
    assert response.data["status"] == "activated"
    assert calls == [
        (
            "thread-1",
            {
                "directory": "/repo",
                "label": "Dottie",
                "agent_name": "Dottie",
                "room_name": invitation.room_name,
            },
        )
    ]


def test_serializers_reject_route_shaping_fields() -> None:
    response = api.user_call(
        _request(
            "/api/user/call/",
            {"agent_name": "Dottie", "room_name": "attacker-room"},
        )
    )
    assert response.status_code == 400
    token_response = livekit.livekit_room_token(
        _request(
            "/api/livekit-room-token/",
            {
                "inbound_invitation_id": INVITATION_ID,
                "room_name": "attacker-room",
            },
        )
    )
    assert token_response.status_code == 400


def test_user_call_rejects_control_characters() -> None:
    response = api.user_call(
        _request("/api/user/call/", {"agent_name": "Dottie\nSpoof"})
    )
    assert response.status_code == 400
