"""Openbase adapter for generic Super Agents execution-control protocols."""

from __future__ import annotations

from typing import Any

from super_agents.execution_control import (
    ApprovalAuthorizationRequest,
    AuthorizationDecision,
    ExecutionRequest,
)

from .broker import get_voice_lockdown_broker
from .policy import check_safe_baseline


def _unsafe_requested_policy(policy: dict[str, Any]) -> str | None:
    approval = str(policy.get("approvalPolicy") or "").strip().lower()
    if approval == "never":
        return "approvalPolicy=never is prohibited"
    sandbox = policy.get("sandboxPolicy") or policy.get("sandbox")
    sandbox_type = sandbox.get("type") if isinstance(sandbox, dict) else sandbox
    if str(sandbox_type or "").strip().lower().replace("-", "") == "dangerfullaccess":
        return "danger-full-access is prohibited"
    permission_mode = str(policy.get("permissionMode") or "").strip().lower()
    if permission_mode in {"bypasspermissions", "dontask"}:
        return f"Claude permission mode {permission_mode} is prohibited"
    return None


class LockdownExecutionPolicyGuard:
    async def validate(self, request: ExecutionRequest) -> AuthorizationDecision:
        broker = get_voice_lockdown_broker()
        health, record = broker.health()
        if health in {"unconfigured", "disabled"}:
            return AuthorizationDecision(allowed=True)
        if health != "ready" or record is None:
            return AuthorizationDecision(allowed=False, reason="Lockdown state is unavailable or corrupt.")
        baseline = check_safe_baseline()
        if not baseline.safe:
            return AuthorizationDecision(allowed=False, reason="Configured backend baseline is unsafe.")
        if reason := _unsafe_requested_policy(request.requested_policy):
            return AuthorizationDecision(allowed=False, reason=reason)
        return AuthorizationDecision(allowed=True)


class LockdownApprovalAuthorizer:
    async def authorize(self, request: ApprovalAuthorizationRequest) -> AuthorizationDecision:
        broker = get_voice_lockdown_broker()
        health, record = broker.health()
        if health in {"unconfigured", "disabled"}:
            return AuthorizationDecision(allowed=True)
        if health != "ready" or record is None:
            return AuthorizationDecision(allowed=False, reason="Lockdown state is unavailable or corrupt.")
        if broker.consume_authorization(request):
            return AuthorizationDecision(allowed=True)
        return AuthorizationDecision(
            allowed=False,
            reason="No unexpired one-use authorization matches this exact approval.",
        )


def managed_execution_controls() -> dict[str, Any]:
    return {
        "execution_policy_guard": LockdownExecutionPolicyGuard(),
        "approval_authorizer": LockdownApprovalAuthorizer(),
        "require_controls": True,
    }
