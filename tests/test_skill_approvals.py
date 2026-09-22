from __future__ import annotations

from pathlib import Path

from super_agents.app_server_client import read_permission_store

from openbase_coder_cli.skill_approvals import (
    answer_skill_approval_request,
    consume_skill_approval_decision,
    create_skill_approval_request,
    get_skill_approval_decision,
    get_skill_approval_request,
    list_skill_approval_requests,
    wait_for_skill_approval,
)


def test_skill_approval_lifecycle_uses_json_store(tmp_path: Path) -> None:
    path = tmp_path / "skill-approvals.json"

    request = create_skill_approval_request(
        skill="whatsapp-cli",
        action="send-message",
        description="Queue a WhatsApp message",
        command="whatsapp-local send contact hello",
        details={"contact": "contact"},
        path=path,
    )

    assert request["id"].startswith("skill-")
    assert list_skill_approval_requests(path) == [request]
    assert get_skill_approval_request(request["id"], path) == request
    store = read_permission_store(path)
    assert request["id"] in store["requests"]
    assert store["requests"][request["id"]]["method"] == "openbaseSkill/requestApproval"

    decision = answer_skill_approval_request(request["id"], "accept", path)

    assert decision["accepted"] is True
    assert decision["decision"] == "accept"
    assert list_skill_approval_requests(path) == []
    assert get_skill_approval_decision(request["id"], path) == decision
    assert request["id"] in read_permission_store(path)["decisions"]

    consumed = consume_skill_approval_decision(request["id"], path)

    assert consumed == decision
    assert request["id"] not in read_permission_store(path)["requests"]
    assert request["id"] not in read_permission_store(path)["decisions"]


def test_skill_approval_timeout_removes_pending_request(tmp_path: Path) -> None:
    path = tmp_path / "skill-approvals.json"
    request = create_skill_approval_request(
        skill="whatsapp-cli",
        action="approve-contact",
        description="Approve a contact",
        path=path,
    )

    decision = wait_for_skill_approval(
        request["id"],
        timeout_seconds=0,
        poll_interval_seconds=0.1,
        path=path,
    )

    assert decision["accepted"] is False
    assert decision["decision"] == "timeout"
    assert list_skill_approval_requests(path) == []
    assert request["id"] not in read_permission_store(path)["requests"]


def test_answered_decision_survives_waiter_death_and_is_consumable(tmp_path: Path) -> None:
    """A decision recorded while nobody waits (the waiter was killed by a
    tool timeout) must still be there for a re-attached wait to consume."""
    path = tmp_path / "skill-approvals.json"
    request = create_skill_approval_request(
        skill="approval-spike",
        action="strand",
        description="Waiter killed before answer",
        path=path,
    )

    answer_skill_approval_request(request["id"], "accept", path)

    decision = wait_for_skill_approval(
        request["id"],
        timeout_seconds=5,
        poll_interval_seconds=0.1,
        path=path,
    )
    assert decision["accepted"] is True
    assert request["id"] not in read_permission_store(path)["requests"]


def test_create_sweeps_stale_skill_residue(tmp_path: Path) -> None:
    import json

    path = tmp_path / "skill-approvals.json"
    stale_answered = create_skill_approval_request(
        skill="old-skill",
        action="answered-long-ago",
        description="Stale answered residue",
        path=path,
    )
    answer_skill_approval_request(stale_answered["id"], "accept", path)
    stale_unanswered = create_skill_approval_request(
        skill="old-skill",
        action="never-answered",
        description="Stale unanswered residue",
        path=path,
    )
    store = json.loads(path.read_text(encoding="utf-8"))
    store["decisions"][stale_answered["id"]]["decidedAt"] = "2020-01-01T00:00:00.000Z"
    store["requests"][stale_unanswered["id"]]["receivedAt"] = "2020-01-01T00:00:00.000Z"
    path.write_text(json.dumps(store), encoding="utf-8")

    fresh = create_skill_approval_request(
        skill="new-skill",
        action="fresh",
        description="Fresh request triggers the sweep",
        path=path,
    )

    store = read_permission_store(path)
    assert stale_answered["id"] not in store["requests"]
    assert stale_answered["id"] not in store["decisions"]
    assert stale_unanswered["id"] not in store["requests"]
    assert fresh["id"] in store["requests"]
