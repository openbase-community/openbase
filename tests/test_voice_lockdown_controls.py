from __future__ import annotations

from super_agents.execution_control import (
    ApprovalAuthorizationRequest,
    ExecutionRequest,
)

from openbase_coder_cli.voice_lockdown.execution_controls import (
    LockdownApprovalAuthorizer,
    LockdownExecutionPolicyGuard,
)


class FakeBroker:
    def __init__(self, health="ready", consumed=True):
        self._health = health
        self._consumed = consumed

    def health(self):
        return self._health, object() if self._health in {"ready", "disabled"} else None

    def consume_authorization(self, request):
        return self._consumed


async def test_enabled_guard_rejects_yolo_and_danger(monkeypatch):
    broker = FakeBroker()
    monkeypatch.setattr(
        "openbase_coder_cli.voice_lockdown.execution_controls.get_voice_lockdown_broker",
        lambda: broker,
    )
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "codex")
    monkeypatch.setenv("LIVEKIT_CODEX_APPROVAL_POLICY", "on-request")
    monkeypatch.setenv("LIVEKIT_CODEX_SANDBOX", "read-only")
    guard = LockdownExecutionPolicyGuard()
    request = ExecutionRequest("codex", "start_turn", {"approvalPolicy": "never"}, "digest")
    assert not (await guard.validate(request)).allowed
    request = ExecutionRequest("codex", "start_turn", {"sandbox": "danger-full-access"}, "digest")
    assert not (await guard.validate(request)).allowed


async def test_indeterminate_state_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "openbase_coder_cli.voice_lockdown.execution_controls.get_voice_lockdown_broker",
        lambda: FakeBroker(health="indeterminate"),
    )
    guard = LockdownExecutionPolicyGuard()
    assert not (await guard.validate(ExecutionRequest("codex", "start_turn", {}, "digest"))).allowed
    authorizer = LockdownApprovalAuthorizer()
    request = ApprovalAuthorizationRequest("codex", "r", "shell", "digest")
    assert not (await authorizer.authorize(request)).allowed


async def test_decline_bypasses_authorizer_in_super_agents():
    # Decline/cancel bypass is exercised at the generic client boundary in
    # Super Agents; this assertion documents that the product authorizer only
    # handles accept requests and cannot turn a denial into an approval.
    authorizer = LockdownApprovalAuthorizer()
    assert not hasattr(authorizer, "decline")
