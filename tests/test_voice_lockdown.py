from __future__ import annotations

import base64
import json
import sqlite3
import threading
from dataclasses import replace

import pytest
from super_agents.execution_control import ApprovalAuthorizationRequest

from openbase_coder_cli.voice_lockdown.broker import (
    ApprovalScope,
    VoiceLockdownBroker,
    canonical_action_digest,
)
from openbase_coder_cli.voice_lockdown.keychain import KeychainRecord
from openbase_coder_cli.voice_lockdown.policy import (
    check_managed_mcp_registration,
    check_safe_baseline,
)
from openbase_coder_cli.voice_lockdown.verifier import (
    derive_verifier,
    normalize_phrase,
    validate_new_phrase,
)

PHRASE = "amber river cedar lantern velvet harbor"


class FakeKeychain:
    def __init__(self, record=None):
        self.record = record

    def read(self):
        return self.record

    def write(self, record):
        self.record = record


def _record(*, enabled=True):
    salt = b"0123456789abcdef"
    return KeychainRecord(
        enabled=enabled,
        salt=base64.b64encode(salt).decode(),
        verifier=derive_verifier(normalize_phrase(PHRASE), salt),
        audit_key=base64.b64encode(b"a" * 32).decode(),
    )


def _scope():
    action = {"method": "item/commandExecution/requestApproval", "tool": "shell", "input": {"command": "true"}}
    return ApprovalScope(
        backend="codex",
        request_id="request-1",
        thread_id="thread-1",
        turn_id="turn-1",
        tool_call_id="tool-call-1",
        tool_name="shell",
        action_digest=canonical_action_digest(action),
        room_sid="room-1",
        participant_identity="owner-1",
    )


def _request(scope):
    return ApprovalAuthorizationRequest(
        backend=scope.backend,
        request_id=scope.request_id,
        action_type=scope.tool_name,
        action_digest=scope.action_digest,
        thread_id=scope.thread_id,
        turn_id=scope.turn_id,
        tool_call_id=scope.tool_call_id,
    )


def _broker(db_path, *, record=None, clock=None):
    keychain = FakeKeychain()
    broker = VoiceLockdownBroker(
        db_path=db_path,
        keychain=keychain,
        **({"clock": clock} if clock is not None else {}),
    )
    broker.set_configuration(record or _record(), event_type="configured")
    return broker


@pytest.fixture(autouse=True)
def safe_codex_baseline(monkeypatch):
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "codex")
    monkeypatch.setenv("LIVEKIT_CODEX_APPROVAL_POLICY", "on-request")
    monkeypatch.setenv("LIVEKIT_CODEX_SANDBOX", "read-only")


def test_phrase_matching_is_exact_whole_utterance():
    assert normalize_phrase("  Amber   River Cedar Lantern Velvet Harbor?! ") == PHRASE
    assert normalize_phrase(f"please {PHRASE}") != PHRASE
    assert normalize_phrase(f"{PHRASE} now") != PHRASE
    assert validate_new_phrase(PHRASE) == PHRASE


def test_default_baseline_is_rejected():
    check = check_safe_baseline(
        {
            "OPENBASE_CODING_BACKEND": "codex",
            "LIVEKIT_CODEX_APPROVAL_POLICY": "never",
            "LIVEKIT_CODEX_SANDBOX": "danger-full-access",
        }
    )
    assert not check.safe
    assert any("approval policy is unsafe" in reason for reason in check.reasons)
    assert any("sandbox is unsafe" in reason for reason in check.reasons)


def test_enable_preflight_rejects_unguarded_mcp_registration(tmp_path):
    codex = tmp_path / "config.toml"
    claude = tmp_path / ".claude.json"
    codex.write_text('[mcp_servers.super-agents]\ncommand = "super-agents-mcp"\n')
    claude.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "super-agents": {
                        "command": "/managed/bin/openbase-coder",
                        "args": ["super-agents-mcp"],
                    }
                }
            }
        )
    )
    check = check_managed_mcp_registration(codex_config=codex, claude_config=claude)
    assert not check.ready
    codex.write_text(
        '[mcp_servers.super-agents]\ncommand = "/managed/bin/openbase-coder"\nargs = ["super-agents-mcp"]\n'
    )
    assert check_managed_mcp_registration(
        codex_config=codex, claude_config=claude
    ).ready


def test_one_phrase_issues_one_exact_30_second_capability(tmp_path):
    now = [100.0]
    broker = _broker(tmp_path / "lockdown.sqlite3", clock=lambda: now[0])
    scope = _scope()
    broker.create_challenge(scope, action_summary="Run a command")
    assert broker.verify_final_utterance(
        f"please {PHRASE}", room_sid=scope.room_sid, participant_identity=scope.participant_identity
    ) == "mismatch"
    assert broker.verify_final_utterance(
        PHRASE, room_sid=scope.room_sid, participant_identity=scope.participant_identity
    ) == "authorized"
    assert broker.consume_authorization(_request(scope))
    assert not broker.consume_authorization(_request(scope))

    broker.create_challenge(scope, action_summary="Run a command")
    assert broker.verify_final_utterance(
        PHRASE, room_sid=scope.room_sid, participant_identity=scope.participant_identity
    ) == "authorized"
    now[0] += 31
    assert not broker.consume_authorization(_request(scope))


def test_capability_rejects_every_scope_substitution(tmp_path):
    broker = _broker(tmp_path / "lockdown.sqlite3")
    scope = _scope()
    broker.create_challenge(scope, action_summary="Run a command")
    assert broker.verify_final_utterance(
        PHRASE, room_sid=scope.room_sid, participant_identity=scope.participant_identity
    ) == "authorized"
    for field, value in (
        ("backend", "claude_code"),
        ("request_id", "other"),
        ("thread_id", "other"),
        ("turn_id", "other"),
        ("tool_call_id", "other"),
        ("tool_name", "other"),
        ("action_digest", "0" * 64),
    ):
        assert not broker.consume_authorization(_request(replace(scope, **{field: value})))
    assert broker.consume_authorization(_request(scope))


def test_atomic_consumption_has_one_winner_and_persists_no_phrase(tmp_path):
    db_path = tmp_path / "lockdown.sqlite3"
    broker = _broker(db_path)
    scope = _scope()
    broker.create_challenge(scope, action_summary="Run a command")
    broker.verify_final_utterance(
        PHRASE, room_sid=scope.room_sid, participant_identity=scope.participant_identity
    )
    results: list[bool] = []

    def consume():
        results.append(broker.consume_authorization(_request(scope)))

    threads = [threading.Thread(target=consume) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1
    assert PHRASE.encode() not in db_path.read_bytes()
    assert PHRASE not in json.dumps(broker.recent_audit())


def test_missing_keychain_after_configuration_is_indeterminate(tmp_path):
    keychain = FakeKeychain()
    broker = VoiceLockdownBroker(db_path=tmp_path / "lockdown.sqlite3", keychain=keychain)
    broker.set_configuration(_record(enabled=False), event_type="configured")
    keychain.record = None
    assert broker.status()["health"] == "indeterminate"


def test_tampered_audit_chain_fails_closed(tmp_path):
    broker = _broker(tmp_path / "lockdown.sqlite3")
    with sqlite3.connect(broker.db_path) as db:
        db.execute("UPDATE audit_events SET detail_json = '{}' WHERE sequence = 1")
    assert broker.status()["health"] == "indeterminate"


def test_tampered_capability_state_cannot_authorize(tmp_path):
    broker = _broker(tmp_path / "lockdown.sqlite3")
    scope = _scope()
    broker.create_challenge(scope, action_summary="Run a command")
    broker.verify_final_utterance(
        PHRASE, room_sid=scope.room_sid, participant_identity=scope.participant_identity
    )
    with sqlite3.connect(broker.db_path) as db:
        db.execute("UPDATE challenges SET capability_mac = ? WHERE state = 'ready'", ("0" * 64,))
    assert not broker.consume_authorization(_request(scope))


def test_tampered_challenge_scope_cannot_verify_phrase(tmp_path):
    broker = _broker(tmp_path / "lockdown.sqlite3")
    scope = _scope()
    broker.create_challenge(scope, action_summary="Run a command")
    with sqlite3.connect(broker.db_path) as db:
        db.execute(
            "UPDATE challenges SET scope_json = replace(scope_json, 'request-1', 'request-2')"
        )
    assert (
        broker.verify_final_utterance(
            PHRASE,
            room_sid=scope.room_sid,
            participant_identity=scope.participant_identity,
        )
        == "not_armed"
    )
