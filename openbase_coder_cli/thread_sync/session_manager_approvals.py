"""Approval-request plumbing mixin for the session manager.

`SessionManagerApprovalsMixin` groups the pending-approval listing and
answering methods. Pure structural extraction; every method is unchanged and
reaches sibling state through ``self``.
"""

from __future__ import annotations

from typing import Any, Literal

from super_agents.app_permissions import permission_response_for_request
from super_agents.app_server_client import (
    shared_permission_requests,
    write_shared_permission_decision,
)

from .session_manager_base import (
    _ensure_client_connected,
    _find_shared_permission_request,
    logger,
)
from .thread_payloads import (
    _approval_request_payload,
)


class SessionManagerApprovalsMixin:
    """Pending app-server approval-request plumbing."""

    async def list_approval_requests(self) -> list[dict[str, Any]]:
        """List currently pending app-server approval requests across threads."""
        requests_by_id = {
            str(request.get("id")): request
            for request in shared_permission_requests()
            if request.get("id") is not None
        }
        try:
            await _ensure_client_connected(self._client)
            for request in self._client.pending_permission_requests():
                payload = _approval_request_payload(request)
                if payload.get("id") is not None:
                    requests_by_id[str(payload["id"])] = payload
        except Exception:
            logger.debug("Unable to merge in-process approval requests", exc_info=True)
        return [
            _approval_request_payload(request) for request in requests_by_id.values()
        ]

    async def answer_approval_request(
        self,
        request_id: str | int,
        decision: Literal["accept", "decline", "cancel"],
    ) -> dict[str, Any]:
        """Answer one pending app-server approval request."""
        await _ensure_client_connected(self._client)
        request = self._find_pending_approval_request(request_id)
        if request is None:
            shared_request = _find_shared_permission_request(request_id)
            if write_shared_permission_decision(request_id, decision):
                return {
                    "answered": False,
                    "queued": True,
                    "requestId": request_id,
                    "result": permission_response_for_request(
                        shared_request or {"method": ""},
                        decision,
                    ),
                }
            raise ValueError(f"No pending approval request found for id {request_id}.")
        return await self._client.answer_request(
            request.id,
            permission_response_for_request(request, decision),
        )

    def _find_pending_approval_request(self, request_id: str | int) -> Any | None:
        candidates: list[str | int] = [request_id]
        if isinstance(request_id, str) and request_id.isdigit():
            candidates.append(int(request_id))
        candidate_strings = {str(item) for item in candidates}
        for request in self._client.pending_permission_requests():
            if request.id in candidates or str(request.id) in candidate_strings:
                return request
        return None
