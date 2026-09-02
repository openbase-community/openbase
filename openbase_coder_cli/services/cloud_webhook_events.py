"""Relay client for cloud-received webhook events.

Openbase Cloud stores provider webhooks durably at capability-token URLs
(``/api/openbase/hooks/t/<token>/``) and acks providers immediately. This
module is the runtime side: create relay endpoints, poll pending events, hand
each one to the local loop-trigger pipeline, and ack what was handled.

Events whose relay endpoint doesn't match a local trigger are left pending —
they belong to another of the user's devices, which will pick them up.
"""

from __future__ import annotations

import base64
from typing import Any

from openbase_coder_cli.services.cloud_registration import (
    CloudReportResult,
    _post_to_cloud,
)

HOOK_ENDPOINTS_PATH = "/api/openbase/hooks/endpoints/"
HOOK_EVENTS_PENDING_PATH = "/api/openbase/hooks/events/pending/"
HOOK_EVENTS_ACK_PATH = "/api/openbase/hooks/events/ack/"

# Delivery outcomes that are final for this device: the event reached the
# trigger pipeline and was decided. Transport errors are NOT in this set, so
# those events stay pending and retry on the next poll.
_FINAL_DELIVERY_STATUSES = {
    "delivered",
    "duplicate",
    "filtered",
    "disabled",
    "unauthorized_sender",
    "unknown_token",
    "rejected",
}


def create_relay_endpoint(
    description: str = "", device_id: str = ""
) -> CloudReportResult:
    payload: dict[str, Any] = {}
    if description:
        payload["description"] = description
    if device_id:
        payload["deviceId"] = device_id
    return _post_to_cloud(HOOK_ENDPOINTS_PATH, payload)


def fetch_pending_relay_events(limit: int = 25) -> CloudReportResult:
    return _post_to_cloud(
        f"{HOOK_EVENTS_PENDING_PATH}?limit={int(limit)}", {}, method="GET"
    )


def ack_relay_events(ids: list[str]) -> CloudReportResult:
    return _post_to_cloud(HOOK_EVENTS_ACK_PATH, {"ids": ids})


async def deliver_relay_events(client, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Deliver polled events to local loop triggers; returns a sweep summary.

    ``client`` is a Super Agents ``CodexAppServerClient`` (or equivalent). The
    relayEndpointId on each local webhook trigger links a cloud event to the
    trigger whose token drives local delivery.
    """
    state = await client.read_state()
    tokens_by_endpoint: dict[str, str] = {}
    for routine in state.routines.values():
        for trigger in routine.triggers or []:
            if trigger.relay_endpoint_id and trigger.token:
                tokens_by_endpoint[trigger.relay_endpoint_id] = trigger.token

    handled_ids: list[str] = []
    delivered = 0
    for event in events:
        endpoint_id = event.get("endpointId")
        token = tokens_by_endpoint.get(endpoint_id) if endpoint_id else None
        if not token:
            continue
        body = base64.b64decode(event.get("bodyBase64") or "")
        result = await client.deliver_webhook_event(
            token,
            headers=event.get("headers") or {},
            body=body,
            origin="cloud",
        )
        status = result.get("status")
        if status in _FINAL_DELIVERY_STATUSES:
            handled_ids.append(event["id"])
        if status == "delivered":
            delivered += 1

    acked = 0
    if handled_ids:
        ack_result = ack_relay_events(handled_ids)
        if ack_result.ok and isinstance(ack_result.response, dict):
            acked = int(ack_result.response.get("acked") or 0)

    return {
        "fetched": len(events),
        "matched": len(handled_ids),
        "delivered": delivered,
        "acked": acked,
    }
