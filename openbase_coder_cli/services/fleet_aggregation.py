"""Cross-device ("fleet") aggregation of threads and reports.

The serving desktop fans out to the user's other Openbase desktops over the
tailnet, merges their results with its own, and returns one combined response.
Peers need no new code or configuration: they answer their existing
``/api/threads/`` and ``/api/projects/reports/all/`` endpoints, authorized by
an owner JWT this CLI mints from its own login (every desktop of one user pins
to the same owner identity). Offline, unreachable, or foreign-account peers
are skipped silently — the local results always come back.

Thread pagination stays server-driven: each source (local + each peer) keeps
its own page/cursor position, and the combined position is carried in one
opaque composite cursor so clients keep their existing single-``next``
infinite scroll. Duplicate threads (thread ids are stable across device sync)
are dropped within each merge window, preferring the local copy on a
timestamp tie; clients' existing id-keyed merging absorbs any duplicate that
slips across window boundaries.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlsplit

import httpx

from openbase_coder_cli.services.tailnet_devices import (
    OPENBASE_CODER_TAILNET_PORT,
    _devices_from_tailscale_status,
    _tailscale_status_payload,
    _url_host_literal,
)

logger = logging.getLogger(__name__)

FLEET_SCOPE_PARAM = "scope"
FLEET_SCOPE_VALUE = "fleet"
PEER_TIMEOUT_SECONDS = 2.5
PEER_LIST_CACHE_SECONDS = 30.0
PEER_FAILURE_CACHE_SECONDS = 60.0
# Cap on refetch rounds while filling one aggregate page, so a pathological
# interleaving can't turn one request into an unbounded crawl of peer pages.
MAX_FILL_ROUNDS = 5

LOCAL_SOURCE_KEY = "local"
ORIGIN_DEVICE_KEY = "origin_device"
# MagicDNS host of the owning peer; clients connect to it DIRECTLY (REST and
# WebSockets) for peer-only items instead of proxying through this server.
ORIGIN_HOST_KEY = "origin_host"


class FleetPeer(NamedTuple):
    key: str
    name: str
    base_url: str


class SourcePage(NamedTuple):
    items: list[dict[str, Any]]
    next_cursor: str | None


@dataclass
class _SourceState:
    """One source's scroll position within a fleet pagination session."""

    page: int = 1
    cursor: str | None = None
    offset: int = 0
    done: bool = False
    # Fetch-round scratch (never serialized into the composite cursor): the
    # unconsumed remainder of the current page and where it continues.
    window: list[dict[str, Any]] | None = field(default=None, compare=False)
    window_next_cursor: str | None = field(default=None, compare=False)


_peer_cache: dict[str, Any] = {"expires": 0.0, "peers": []}
_peer_failures: dict[str, float] = {}


def fleet_peers() -> list[FleetPeer]:
    """Online tailnet peers, addressed at the Openbase tailnet port.

    Cached briefly so list polling doesn't shell out to ``tailscale status``
    on every page. Peers recently seen unreachable are excluded until their
    failure entry expires.
    """
    now = time.monotonic()
    if now < _peer_cache["expires"]:
        peers = _peer_cache["peers"]
    else:
        _, status_payload, _ = _tailscale_status_payload()
        peers = []
        if status_payload is not None:
            for device in _devices_from_tailscale_status(status_payload):
                if device.is_self or not device.online:
                    continue
                base = (
                    f"http://{_url_host_literal(device.host)}"
                    f":{OPENBASE_CODER_TAILNET_PORT}"
                )
                peers.append(
                    FleetPeer(key=device.host, name=device.name, base_url=base)
                )
        _peer_cache["peers"] = peers
        _peer_cache["expires"] = now + PEER_LIST_CACHE_SECONDS
    return [peer for peer in peers if _peer_failures.get(peer.key, 0.0) <= now]


def _mark_peer_failed(peer: FleetPeer, error: Exception | str) -> None:
    _peer_failures[peer.key] = time.monotonic() + PEER_FAILURE_CACHE_SECONDS
    logger.info("fleet: skipping peer %s (%s)", peer.name, error)


def owner_access_token() -> str | None:
    """A cloud JWT for the signed-in owner, or None when not logged in.

    Every desktop belonging to one user pins to the same owner identity, so
    this token authorizes against all of them. Without it there is nothing to
    fan out with — callers fall back to local-only results.
    """
    from openbase_coder_cli.cloud_environment import configured_web_backend_url
    from openbase_coder_cli.config.token_manager import (
        AuthLoginRequiredError,
        AuthTransientError,
        TokenManager,
    )

    manager = TokenManager(configured_web_backend_url())
    try:
        return manager.get_access_token()
    except (AuthLoginRequiredError, AuthTransientError) as exc:
        # Genuine fallback: an unauthenticated install still serves its own
        # threads/reports; it just cannot reach peers.
        logger.info("fleet: no owner token available (%s)", exc)
        return None


def peer_get(
    peer: FleetPeer,
    path: str,
    token: str,
    *,
    params: dict[str, str] | None = None,
) -> httpx.Response | None:
    """One authenticated GET against a peer; None (and a failure mark) on error."""
    try:
        response = httpx.get(
            f"{peer.base_url}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=PEER_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        _mark_peer_failed(peer, exc)
        return None
    # 401/403 means the peer will not authorize this owner (foreign account)
    # and 5xx means it is unwell — both are worth backing off from. Other
    # statuses (e.g. a 404 detail probe) are answers, not failures.
    if response.status_code in (401, 403) or response.status_code >= 500:
        _mark_peer_failed(peer, f"HTTP {response.status_code}")
        return None
    return response


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


def thread_payload_sort_key(item: dict[str, Any]) -> datetime:
    current_turn = item.get("current_turn") or {}
    raw = current_turn.get("started_at") or item.get("updated_at") or ""
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _cursor_from_next_url(next_url: str | None) -> str | None:
    if not next_url:
        return None
    query = parse_qs(urlsplit(str(next_url)).query)
    values = query.get("cursor")
    return values[0] if values else None


def _fetch_peer_thread_page(
    peer: FleetPeer,
    token: str,
    *,
    page: int,
    page_size: int,
    cursor: str | None,
) -> SourcePage | None:
    params = {"page": str(page), "page_size": str(page_size)}
    if cursor:
        params["cursor"] = cursor
    response = peer_get(peer, "/api/threads/", token, params=params)
    if response is None:
        return None
    try:
        payload = response.json()
    except ValueError as exc:
        _mark_peer_failed(peer, exc)
        return None
    threads = payload.get("threads")
    if not isinstance(threads, list):
        _mark_peer_failed(peer, "malformed thread list payload")
        return None
    items = [item for item in threads if isinstance(item, dict)]
    for item in items:
        item[ORIGIN_DEVICE_KEY] = peer.name
        item[ORIGIN_HOST_KEY] = peer.key
    return SourcePage(
        items=items, next_cursor=_cursor_from_next_url(payload.get("next"))
    )


def encode_fleet_cursor(states: dict[str, _SourceState]) -> str:
    payload = {
        "v": 1,
        "sources": {
            key: {
                "page": state.page,
                "cursor": state.cursor,
                "offset": state.offset,
                "done": state.done,
            }
            for key, state in states.items()
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_fleet_cursor(cursor: str | None) -> dict[str, _SourceState] | None:
    """Decode a composite cursor; None for a first page or an unreadable one."""
    if not cursor:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(payload, dict):
        return None
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        return None
    states: dict[str, _SourceState] = {}
    for key, value in sources.items():
        if not isinstance(value, dict):
            return None
        states[key] = _SourceState(
            page=int(value.get("page", 1)),
            cursor=value.get("cursor") or None,
            offset=int(value.get("offset", 0)),
            done=bool(value.get("done", False)),
        )
    return states


class FleetThreadPage(NamedTuple):
    threads: list[dict[str, Any]]
    next_cursor: str | None


def fleet_thread_page(
    *,
    page_size: int,
    cursor: str | None,
    fetch_local_page,
) -> FleetThreadPage:
    """Merge one page of threads across the local install and every peer.

    ``fetch_local_page(page, cursor, page_size) -> SourcePage | None`` serves
    the local source without an HTTP self-call. Peers fetch concurrently.

    Emission follows the sorted-streams merge rule: the globally newest head
    is only taken while every unfinished source has a non-empty fetched
    window. Each source's unfetched remainder is entirely older than its
    fetched window, so under that condition the taken head can never be
    preceded by an unseen newer item. When a window empties, the source
    advances to its next page (or finishes) and the merge resumes.
    """
    token = owner_access_token()
    peers = {peer.key: peer for peer in fleet_peers()} if token else {}

    states = decode_fleet_cursor(cursor) or {}
    if not states:
        states = {LOCAL_SOURCE_KEY: _SourceState()}
        for key in peers:
            states[key] = _SourceState()
    # A peer that appeared after the cursor was minted starts mid-scroll;
    # leave it out of this scroll session rather than splicing unseen-newer
    # items into an older window. A peer that disappeared just finishes.
    active_keys = [key for key in states if key == LOCAL_SOURCE_KEY or key in peers]
    for key in states:
        if key not in active_keys:
            states[key].done = True

    def fetch_window(key: str) -> None:
        state = states[key]
        if state.done or state.window is not None:
            return
        if key == LOCAL_SOURCE_KEY:
            source_page = fetch_local_page(state.page, state.cursor, page_size)
        else:
            source_page = _fetch_peer_thread_page(
                peers[key],
                token or "",
                page=state.page,
                page_size=page_size,
                cursor=state.cursor,
            )
        if source_page is None:
            state.done = True
            return
        state.window = source_page.items[state.offset :]
        state.window_next_cursor = source_page.next_cursor

    def refill() -> None:
        pending = [
            key
            for key in active_keys
            if not states[key].done and states[key].window is None
        ]
        if not pending:
            return
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(pending)))) as pool:
            list(pool.map(fetch_window, pending))
        # An empty page with a continuation is possible in principle; advance
        # past it here so the merge below only ever sees real windows.
        for key in pending:
            _advance_if_consumed(states[key])

    def _advance_if_consumed(state: _SourceState) -> None:
        if state.done or state.window is None or state.window:
            return
        if state.window_next_cursor:
            state.page += 1
            state.cursor = state.window_next_cursor
            state.offset = 0
            state.window = None
            state.window_next_cursor = None
        else:
            state.done = True

    emitted: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for _ in range(MAX_FILL_ROUNDS):
        if len(emitted) >= page_size:
            break
        refill()
        open_states = [states[key] for key in active_keys if not states[key].done]
        if not open_states:
            break
        # Merge while every unfinished source has a non-empty window.
        while len(emitted) < page_size:
            if any(not state.window for state in open_states if not state.done):
                break
            best_key = None
            best_sort = None
            for key in active_keys:
                state = states[key]
                if state.done or not state.window:
                    continue
                sort_value = thread_payload_sort_key(state.window[0])
                if (
                    best_sort is None
                    or sort_value > best_sort
                    or (sort_value == best_sort and key == LOCAL_SOURCE_KEY)
                ):
                    best_key = key
                    best_sort = sort_value
            if best_key is None:
                break
            state = states[best_key]
            assert state.window is not None
            item = state.window.pop(0)
            state.offset += 1
            _advance_if_consumed(state)
            thread_id = str(item.get("thread_id") or "")
            if thread_id and thread_id in seen_ids:
                continue
            if thread_id:
                seen_ids.add(thread_id)
            emitted.append(item)

    has_more = any(not states[key].done or states[key].window for key in active_keys)
    next_cursor = (
        encode_fleet_cursor({key: states[key] for key in active_keys})
        if has_more
        else None
    )
    return FleetThreadPage(threads=emitted, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _home_relative_key(project_path: str, file_name: str) -> tuple[str, str]:
    """Dedup key that survives differing home directories across devices."""
    from openbase_coder_cli.thread_sync.thread_sync_common import translate_home_path

    translated = translate_home_path(project_path, target_home=Path("/HOME"))
    return (translated or project_path, file_name)


def fleet_report_items(local_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge report items across the local install and every peer.

    Local items win their dedup slot outright; a peer item survives only when
    no device-agnostic (project, file) twin exists locally or on an earlier
    peer. Peer-only items keep their peer-native paths and carry
    ``origin_device`` so file reads can be proxied back to the owning device.
    """
    token = owner_access_token()
    peers = fleet_peers() if token else []
    if not peers:
        return local_items

    def fetch(peer: FleetPeer) -> list[dict[str, Any]]:
        response = peer_get(peer, "/api/projects/reports/all/", token or "")
        if response is None:
            return []
        try:
            payload = response.json()
        except ValueError as exc:
            _mark_peer_failed(peer, exc)
            return []
        items = payload.get("items")
        if not isinstance(items, list):
            _mark_peer_failed(peer, "malformed reports payload")
            return []
        peer_items = [item for item in items if isinstance(item, dict)]
        for item in peer_items:
            item[ORIGIN_DEVICE_KEY] = peer.name
            item[ORIGIN_HOST_KEY] = peer.key
        return peer_items

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(peers)))) as executor:
        peer_results = list(executor.map(fetch, peers))

    def item_key(item: dict[str, Any]) -> tuple[str, str] | None:
        project = item.get("project") or {}
        file_payload = item.get("file") or {}
        project_path = str(project.get("path") or "")
        relative_path = str(file_payload.get("path") or "")
        if not project_path or not relative_path:
            return None
        return _home_relative_key(project_path, relative_path)

    merged: list[dict[str, Any]] = list(local_items)
    seen = {key for key in (item_key(item) for item in local_items) if key}
    for peer_items in peer_results:
        for item in peer_items:
            key = item_key(item)
            if key is None or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    merged.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return merged


# ---------------------------------------------------------------------------
# Device-local feeds (approvals, notifications, projects)
#
# Unlike threads/reports these are never synced between devices and their ids
# are device-local, so a fleet view is plain concat + origin stamp + sort —
# no dedup. Clients answer/ack/create DIRECTLY against the stamped
# origin_host; nothing is proxied.
# ---------------------------------------------------------------------------


def _iso_sort_key(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _fan_out_peer_payloads(
    path: str,
    *,
    params: dict[str, str] | None = None,
    list_key: str,
) -> list[tuple[FleetPeer, dict[str, Any]]]:
    """Fetch a payload from every reachable peer, ``list_key`` items stamped."""
    token = owner_access_token()
    peers = fleet_peers() if token else []
    if not peers:
        return []

    def fetch(peer: FleetPeer) -> tuple[FleetPeer, dict[str, Any]] | None:
        response = peer_get(peer, path, token or "", params=params)
        if response is None or response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError as exc:
            _mark_peer_failed(peer, exc)
            return None
        if not isinstance(payload, dict) or not isinstance(
            payload.get(list_key), list
        ):
            return None
        for item in payload[list_key]:
            if isinstance(item, dict):
                item[ORIGIN_DEVICE_KEY] = peer.name
                item[ORIGIN_HOST_KEY] = peer.key
        return (peer, payload)

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(peers)))) as executor:
        results = list(executor.map(fetch, peers))
    return [entry for entry in results if entry is not None]


def _peer_list_items(
    payloads: list[tuple[FleetPeer, dict[str, Any]]], list_key: str
) -> list[dict[str, Any]]:
    return [
        item
        for _, payload in payloads
        for item in payload[list_key]
        if isinstance(item, dict)
    ]


def fleet_approval_requests(
    local_requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payloads = _fan_out_peer_payloads("/api/approval-requests/", list_key="requests")
    merged = list(local_requests)
    merged.extend(_peer_list_items(payloads, "requests"))
    merged.sort(key=lambda item: _iso_sort_key(item.get("received_at")), reverse=True)
    return merged


def fleet_notifications(
    local_payload: dict[str, Any],
    *,
    include_read: bool,
    limit: int,
) -> dict[str, Any]:
    params = {"limit": str(limit)}
    if not include_read:
        params["include_read"] = "false"
    payloads = _fan_out_peer_payloads(
        "/api/notifications/", params=params, list_key="notifications"
    )
    notifications = list(local_payload.get("notifications") or [])
    notifications.extend(_peer_list_items(payloads, "notifications"))
    notifications.sort(
        key=lambda item: _iso_sort_key(item.get("created_at")), reverse=True
    )
    unread_count = int(local_payload.get("unread_count") or 0) + sum(
        int(payload.get("unread_count") or 0) for _, payload in payloads
    )
    return {"notifications": notifications[:limit], "unread_count": unread_count}


def fleet_recent_projects(
    local_projects: list[dict[str, Any]],
    *,
    page_size: int,
) -> list[dict[str, Any]]:
    """Top recent projects across the fleet (first page per device only).

    The new-thread picker needs "what have I worked on lately, anywhere" —
    top-N per device merged by recency covers that without cross-device
    pagination. Creating the thread targets the project's origin device.
    """
    payloads = _fan_out_peer_payloads(
        "/api/projects/recent/",
        params={"page_size": str(page_size)},
        list_key="projects",
    )
    merged = list(local_projects)
    merged.extend(_peer_list_items(payloads, "projects"))
    merged.sort(
        key=lambda item: _iso_sort_key(item.get("last_worked_on")), reverse=True
    )
    return merged[:page_size]


# ---------------------------------------------------------------------------
# Peer read proxy
# ---------------------------------------------------------------------------


def find_peer(device_name: str) -> FleetPeer | None:
    for peer in fleet_peers():
        if peer.name == device_name or peer.key == device_name:
            return peer
    return None


def fleet_thread_detail(thread_id: str) -> dict[str, Any] | None:
    """A peer's read-only copy of a thread this device does not hold."""
    token = owner_access_token()
    if not token:
        return None
    for peer in fleet_peers():
        response = peer_get(peer, f"/api/threads/{thread_id}/", token)
        if response is None or response.status_code != 200:
            continue
        try:
            payload = response.json()
        except ValueError as exc:
            _mark_peer_failed(peer, exc)
            continue
        if isinstance(payload, dict):
            payload[ORIGIN_DEVICE_KEY] = peer.name
            payload[ORIGIN_HOST_KEY] = peer.key
            return payload
    return None


class PeerReportResponse(NamedTuple):
    status_code: int
    payload: dict[str, Any] | None
    content: bytes | None
    content_type: str | None
    filename: str | None


def proxy_peer_report_request(
    device_name: str,
    path: str,
    params: dict[str, str],
    *,
    binary: bool = False,
) -> PeerReportResponse | None:
    """Forward a report read to the origin device; None when unreachable.

    Peer-only report items keep peer-native project paths, so the params pass
    through verbatim — the origin device resolves them against its own disk.
    """
    token = owner_access_token()
    if not token:
        return None
    peer = find_peer(device_name)
    if peer is None:
        return None
    response = peer_get(peer, path, token, params=params)
    if response is None:
        return None
    if binary:
        disposition = response.headers.get("content-disposition", "")
        filename = None
        if "filename=" in disposition:
            filename = disposition.split("filename=")[-1].strip('"; ')
        return PeerReportResponse(
            status_code=response.status_code,
            payload=None,
            content=response.content,
            content_type=response.headers.get("content-type"),
            filename=filename,
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return PeerReportResponse(
        status_code=response.status_code,
        payload=payload if isinstance(payload, dict) else None,
        content=None,
        content_type=None,
        filename=None,
    )
