"""Route approval decisions to a discovered peer using the owner's cloud identity."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from openbase_coder_cli.services.fleet_aggregation import (
    PEER_TIMEOUT_SECONDS,
    find_peer,
    owner_access_token,
)


def answer_peer_approval(
    origin_host: str, request_id: str, decision: str
) -> tuple[int, dict]:
    # Resolve a discovered peer rather than trusting a client-supplied URL.
    # Local installation capabilities must never be forwarded to another host.
    peer = find_peer(origin_host)
    if peer is None:
        return 404, {
            "error": "The approval's device is unavailable. Refresh the queue."
        }
    token = owner_access_token()
    if not token:
        return 503, {
            "error": "Sign in to Openbase on this device to answer remote approvals."
        }
    try:
        response = httpx.post(
            f"{peer.base_url}/api/approval-requests/{quote(request_id, safe='')}/",
            headers={"Authorization": f"Bearer {token}"},
            # Omit origin_host so the peer handles its own device-local ID.
            json={"decision": decision},
            timeout=PEER_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        # A timed-out write may already have landed. Never retry automatically.
        return 502, {
            "error": "Could not confirm the remote decision. Refresh the queue before retrying."
        }
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or 300 <= response.status_code < 400:
        return 502, {
            "error": "Unexpected response from the approval's device. Refresh the queue."
        }
    return response.status_code, payload
