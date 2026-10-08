"""The user's durable machines: where a thread can be pushed to keep running.

A durable machine is a computer of the same user that stays on while this
one sleeps, shares the synced folders, and runs the Openbase runtime. Today
that is the Openbase Sync **hub** this computer is paired with as an edge
(``role = "edge"``; ``peer_hot`` names the hub). The list is deliberately a
list of typed targets rather than "the hub", so other kinds — for example a
project-only cloud workspace that syncs one project folder — can join it
without changing the push flow or its API.

Peers are reached directly over the Openbase VPN at the runtime's tailnet
port and authorized with the owner's cloud JWT, exactly like fleet reads.
``OPENBASE_HUB_URL`` (shared with ``openbase-coder codex|claude --remote``)
overrides the hub's address, and ``OPENBASE_HUB_TOKEN`` the bearer sent to
it, for development and isolated test rigs.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

KIND_SYNC_HUB = "sync_hub"
HUB_TOKEN_ENV = "OPENBASE_HUB_TOKEN"
TARGET_INFO_PATH = "/api/threads/push/target/"
ARRIVALS_PATH = "/api/threads/push/arrivals/"
PROBE_TIMEOUT_SECONDS = 3.0
ARRIVAL_TIMEOUT_SECONDS = 180.0
FETCH_FAILURE_BACKOFF_SECONDS = 60.0
_fetch_failures: dict[str, float] = {}
# Bumped when the arrival request or response changes incompatibly.
PUSH_PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class DurableTarget:
    """One place a thread can be pushed to."""

    key: str  # what ``--to`` and the API accept; stable per target
    name: str  # shown to people
    host: str  # tailnet host the console connects to directly
    base_url: str  # the target runtime's API base
    kind: str = KIND_SYNC_HUB

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("base_url")
        return data


class TargetError(RuntimeError):
    """Talking to a target failed; ``code`` is stable, the message is for people."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        reached: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        # Whether the request may have been processed by the target (a lost
        # response): the caller must not assume nothing happened.
        self.reached = reached


def this_computer_is_durable() -> bool:
    from openbase_coder_cli.agent_remote import read_sync_facts

    facts = read_sync_facts()
    return facts.configured and facts.role == "hub"


def durable_targets() -> list[DurableTarget]:
    """Every durable machine this computer can push threads to."""
    targets: list[DurableTarget] = []
    hub = _sync_hub_target()
    if hub is not None:
        targets.append(hub)
    return targets


def _sync_hub_target() -> DurableTarget | None:
    from openbase_coder_cli.agent_remote import hub_base_url, read_sync_facts

    facts = read_sync_facts()
    if not facts.configured or facts.role != "edge" or not facts.hub_host:
        return None
    name = facts.hub_host
    host = facts.hub_host
    try:
        from openbase_coder_cli.sync_pairing import find_pairing_peer

        peer = find_pairing_peer(facts.hub_host)
    except Exception:  # noqa: BLE001 - naming is a nicety; an offline hub still counts
        peer = None
    if peer is not None:
        name = peer.name or name
        host = peer.key or host
    return DurableTarget(
        key=host,
        name=name,
        host=host,
        base_url=hub_base_url(facts.hub_host),
        kind=KIND_SYNC_HUB,
    )


def find_target(
    value: str | None, targets: list[DurableTarget] | None = None
) -> DurableTarget | None:
    """The target named by ``value`` (key, name or host), else the first one."""
    targets = durable_targets() if targets is None else targets
    if not value:
        return targets[0] if targets else None
    wanted = value.strip().lower()
    for target in targets:
        if wanted in {target.key.lower(), target.name.lower(), target.host.lower()}:
            return target
    return None


def peer_token() -> str | None:
    override = os.environ.get(HUB_TOKEN_ENV, "").strip()
    if override:
        return override
    from openbase_coder_cli.services.fleet_aggregation import owner_access_token

    return owner_access_token()


def _headers() -> dict[str, str]:
    token = peer_token()
    if not token:
        raise TargetError(
            "not_signed_in",
            "Sign in to Openbase on this computer to reach your other computers.",
        )
    return {"Authorization": f"Bearer {token}"}


def _error_from_response(
    target: DurableTarget, response: httpx.Response, fallback: str
) -> TargetError:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if response.status_code == 404 and not payload.get("code"):
        return TargetError(
            "target_outdated",
            f"{target.name} runs an Openbase version that cannot receive threads. "
            "Update Openbase there and try again.",
        )
    if response.status_code in (401, 403):
        return TargetError(
            "target_unauthorized",
            f"{target.name} did not accept this computer's sign-in. "
            "Both computers must be signed in to the same Openbase account.",
        )
    return TargetError(
        str(payload.get("code") or "target_error"),
        str(payload.get("error") or fallback),
        retryable=bool(payload.get("safe_to_retry")),
    )


def target_info(
    target: DurableTarget, *, timeout: float = PROBE_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """What the target can accept (raises ``TargetError`` when it cannot)."""
    try:
        response = httpx.get(
            f"{target.base_url}{TARGET_INFO_PATH}",
            headers=_headers(),
            timeout=timeout,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise TargetError(
            "target_offline",
            f"{target.name} is not reachable over Openbase VPN. "
            "Make sure it is on and connected, then try again.",
            retryable=True,
        ) from exc
    if response.status_code != 200:
        raise _error_from_response(
            target, response, f"{target.name} cannot receive threads right now."
        )
    try:
        info = response.json()
    except ValueError as exc:
        raise TargetError(
            "target_error", f"{target.name} sent an unreadable answer."
        ) from exc
    if not isinstance(info, dict) or not info.get("accepts_pushes"):
        raise TargetError(
            "target_outdated",
            f"{target.name} cannot receive threads. Update Openbase there.",
        )
    return info


def send_arrival(
    target: DurableTarget,
    payload: dict[str, Any],
    *,
    timeout: float = ARRIVAL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Ask the target to take the thread (idempotent per operation id)."""
    try:
        response = httpx.post(
            f"{target.base_url}{ARRIVALS_PATH}",
            json=payload,
            headers=_headers(),
            timeout=timeout,
            follow_redirects=False,
        )
    except httpx.ConnectError as exc:
        raise TargetError(
            "target_offline",
            f"{target.name} is not reachable over Openbase VPN.",
            retryable=True,
        ) from exc
    except httpx.HTTPError as exc:
        # The request may have been delivered and processed; only a retry
        # with the same operation id can tell.
        raise TargetError(
            "target_unreachable",
            f"Lost the connection to {target.name} while it was taking the "
            "thread. Retry the push to finish it.",
            retryable=True,
            reached=True,
        ) from exc
    if response.status_code != 200:
        raise _error_from_response(
            target, response, f"{target.name} could not take the thread."
        )
    try:
        result = response.json()
    except ValueError as exc:
        raise TargetError(
            "target_error",
            f"{target.name} sent an unreadable answer.",
            reached=True,
            retryable=True,
        ) from exc
    if not isinstance(result, dict) or not result.get("thread_id"):
        raise TargetError(
            "target_error",
            f"{target.name} did not confirm the thread.",
            reached=True,
            retryable=True,
        )
    return result


def home_relative(path: str | Path) -> str:
    from openbase_coder_cli.agent_remote import home_relative as _home_relative

    return _home_relative(Path(path), Path.home())


def fetch_thread(
    target: DurableTarget,
    thread_id: str,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """The target's copy of a thread, tagged with where it lives (or None)."""
    from openbase_coder_cli.services.fleet_aggregation import (
        ORIGIN_DEVICE_KEY,
        ORIGIN_HOST_KEY,
    )

    # Thread detail is polled; an unreachable target must not add its
    # timeout to every poll, so failures back off for a minute.
    if _fetch_failures.get(target.base_url, 0.0) > time.monotonic():
        return None
    try:
        response = httpx.get(
            f"{target.base_url}/api/threads/{thread_id}/",
            headers=_headers(),
            timeout=timeout,
            follow_redirects=False,
        )
        payload = response.json() if response.status_code == 200 else None
    except (TargetError, httpx.HTTPError, ValueError):
        _fetch_failures[target.base_url] = (
            time.monotonic() + FETCH_FAILURE_BACKOFF_SECONDS
        )
        return None
    if not isinstance(payload, dict):
        return None
    payload[ORIGIN_DEVICE_KEY] = target.name
    payload[ORIGIN_HOST_KEY] = target.host
    return payload
