from unittest.mock import Mock

import httpx
import pytest

from openbase_coder_cli.services import peer_approvals
from openbase_coder_cli.services.fleet_aggregation import FleetPeer


@pytest.fixture
def peer(monkeypatch):
    peer = FleetPeer("mini.example", "mini", "http://mini.example:18080")
    monkeypatch.setattr(
        peer_approvals, "find_peer", lambda host: peer if host == peer.key else None
    )
    monkeypatch.setattr(peer_approvals, "owner_access_token", lambda: "owner.cloud.jwt")
    return peer


@pytest.mark.parametrize("decision", ["accept", "decline", "cancel"])
def test_remote_answer_uses_owner_token_and_does_not_forward_origin(
    monkeypatch, peer, decision
):
    post = Mock(return_value=httpx.Response(200, json={"success": True}))
    monkeypatch.setattr(peer_approvals.httpx, "post", post)

    assert peer_approvals.answer_peer_approval(peer.key, "skill:id ?#", decision) == (
        200,
        {"success": True},
    )
    post.assert_called_once_with(
        "http://mini.example:18080/api/approval-requests/skill%3Aid%20%3F%23/",
        headers={"Authorization": "Bearer owner.cloud.jwt"},
        json={"decision": decision},
        timeout=peer_approvals.PEER_TIMEOUT_SECONDS,
        follow_redirects=False,
    )


def test_unknown_host_cannot_receive_owner_token(monkeypatch, peer):
    post = Mock()
    monkeypatch.setattr(peer_approvals.httpx, "post", post)
    status, _ = peer_approvals.answer_peer_approval(
        "http://attacker.example", "id", "accept"
    )
    assert status == 404
    post.assert_not_called()


def test_missing_owner_login_does_not_send_decision(monkeypatch, peer):
    monkeypatch.setattr(peer_approvals, "owner_access_token", lambda: None)
    post = Mock()
    monkeypatch.setattr(peer_approvals.httpx, "post", post)
    assert peer_approvals.answer_peer_approval(peer.key, "id", "accept")[0] == 503
    post.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 404, 409])
def test_peer_rejection_is_preserved(monkeypatch, peer, status):
    payload = {"error": "Peer rejected request"}
    monkeypatch.setattr(
        peer_approvals.httpx,
        "post",
        Mock(return_value=httpx.Response(status, json=payload)),
    )
    assert peer_approvals.answer_peer_approval(peer.key, "id", "accept") == (
        status,
        payload,
    )


def test_timeout_is_not_retried(monkeypatch, peer):
    post = Mock(side_effect=httpx.ReadTimeout("timeout"))
    monkeypatch.setattr(peer_approvals.httpx, "post", post)
    status, payload = peer_approvals.answer_peer_approval(peer.key, "id", "accept")
    assert status == 502
    assert "before retrying" in payload["error"]
    assert post.call_count == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>login</html>"),
        httpx.Response(302, json={}),
        httpx.Response(200, json=[]),
    ],
)
def test_invalid_peer_response_is_not_success(monkeypatch, peer, response):
    monkeypatch.setattr(peer_approvals.httpx, "post", Mock(return_value=response))
    assert peer_approvals.answer_peer_approval(peer.key, "id", "accept")[0] == 502
