"""Readiness of the local livekit-agent worker, for gating room creation."""

from __future__ import annotations

import os

import httpx


def livekit_agent_worker_ready(*, timeout: float = 1.0) -> bool:
    """True when the worker's health server answers 200.

    The worker registers with LiveKit right after that server comes up. A
    room minted before the worker exists carries an agent dispatch nobody
    picks up, and the dispatch is not replayed once the worker registers, so
    the call sits agent-less for good (first call after a cold Cloud
    Workspace wake, 2026-10-07).
    """
    host = os.environ.get("LIVEKIT_AGENT_HOST", "127.0.0.1")
    port = os.environ.get("LIVEKIT_AGENT_PORT", "8081")
    try:
        with httpx.Client(timeout=timeout) as client:
            return client.get(f"http://{host}:{port}/").status_code == 200
    except Exception:  # noqa: BLE001 - any failure to reach the worker means not ready
        return False
