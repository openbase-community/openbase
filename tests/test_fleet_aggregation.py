from __future__ import annotations

from openbase_coder_cli.services import fleet_aggregation as fleet
from openbase_coder_cli.services.fleet_aggregation import (
    FleetPeer,
    SourcePage,
    decode_fleet_cursor,
    fleet_report_items,
    fleet_thread_page,
)


def _thread(thread_id: str, updated_at: str) -> dict:
    return {"thread_id": thread_id, "updated_at": updated_at, "directory": "/tmp"}


def _paged_source(items: list[dict], page_size: int):
    """Cursor-paginated source over a pre-sorted (desc) item list."""

    def fetch(page: int, cursor: str | None, size: int) -> SourcePage:
        start = int(cursor or 0)
        end = start + size
        next_cursor = str(end) if end < len(items) else None
        return SourcePage(items=items[start:end], next_cursor=next_cursor)

    return fetch


def _install_peer(monkeypatch, peer_items: dict[str, list[dict]]) -> None:
    peers = [
        FleetPeer(key=key, name=key, base_url=f"http://{key}:18080")
        for key in peer_items
    ]
    monkeypatch.setattr(fleet, "owner_access_token", lambda: "token")
    monkeypatch.setattr(fleet, "fleet_peers", lambda: peers)

    def fetch_peer(peer, token, *, page, page_size, cursor):
        items = peer_items[peer.key]
        start = int(cursor or 0)
        end = start + page_size
        window = [dict(item) for item in items[start:end]]
        for item in window:
            item[fleet.ORIGIN_DEVICE_KEY] = peer.name
        next_cursor = str(end) if end < len(items) else None
        return SourcePage(items=window, next_cursor=next_cursor)

    monkeypatch.setattr(fleet, "_fetch_peer_thread_page", fetch_peer)


def _drain(page_size: int, fetch_local) -> list[dict]:
    """Collect every page the way clients do: id-deduped on append.

    Within one page the server never repeats a thread id; across pages a
    stale synced copy can reappear, and every client list merges by
    thread_id (first/newest occurrence wins), which this mirrors.
    """
    collected: list[dict] = []
    seen: set[str] = set()
    cursor: str | None = None
    for _ in range(20):
        result = fleet_thread_page(
            page_size=page_size, cursor=cursor, fetch_local_page=fetch_local
        )
        page_ids = [item["thread_id"] for item in result.threads]
        assert len(page_ids) == len(set(page_ids)), "dup within one page"
        for item in result.threads:
            if item["thread_id"] in seen:
                continue
            seen.add(item["thread_id"])
            collected.append(item)
        cursor = result.next_cursor
        if cursor is None:
            break
    assert cursor is None
    return collected


def test_fleet_page_merges_sources_newest_first(monkeypatch):
    local = [
        _thread("a", "2026-09-12T10:00:00+00:00"),
        _thread("b", "2026-09-12T08:00:00+00:00"),
        _thread("c", "2026-09-12T06:00:00+00:00"),
    ]
    _install_peer(
        monkeypatch,
        {
            "mini": [
                _thread("d", "2026-09-12T09:00:00+00:00"),
                _thread("e", "2026-09-12T07:00:00+00:00"),
                _thread("f", "2026-09-12T05:00:00+00:00"),
            ]
        },
    )

    collected = _drain(2, _paged_source(local, 2))

    assert [item["thread_id"] for item in collected] == [
        "a",
        "d",
        "b",
        "e",
        "c",
        "f",
    ]
    origins = {item["thread_id"]: item.get("origin_device") for item in collected}
    assert origins["a"] is None
    assert origins["d"] == "mini"


def test_fleet_page_dedupes_synced_threads_preferring_local(monkeypatch):
    shared_new = "2026-09-12T10:00:00+00:00"
    local = [
        _thread("a", shared_new),
        _thread("b", "2026-09-12T08:00:00+00:00"),
    ]
    # The peer holds the same threads (device sync), one at an equal
    # timestamp and one staler.
    _install_peer(
        monkeypatch,
        {
            "mini": [
                _thread("a", shared_new),
                _thread("b", "2026-09-12T07:00:00+00:00"),
            ]
        },
    )

    collected = _drain(2, _paged_source(local, 2))

    assert [item["thread_id"] for item in collected] == ["a", "b"]
    assert all(item.get("origin_device") is None for item in collected)


def test_fleet_page_surfaces_peer_only_threads(monkeypatch):
    local = [_thread("a", "2026-09-12T10:00:00+00:00")]
    _install_peer(
        monkeypatch,
        {
            "mini": [
                _thread("a", "2026-09-12T10:00:00+00:00"),
                _thread("old", "2026-06-01T00:00:00+00:00"),
            ]
        },
    )

    collected = _drain(5, _paged_source(local, 5))

    assert [item["thread_id"] for item in collected] == ["a", "old"]
    assert collected[1]["origin_device"] == "mini"


def test_fleet_page_without_token_is_local_only(monkeypatch):
    monkeypatch.setattr(fleet, "owner_access_token", lambda: None)
    local = [
        _thread("a", "2026-09-12T10:00:00+00:00"),
        _thread("b", "2026-09-12T09:00:00+00:00"),
    ]

    collected = _drain(1, _paged_source(local, 1))

    assert [item["thread_id"] for item in collected] == ["a", "b"]


def test_fleet_page_survives_peer_failure_mid_scroll(monkeypatch):
    local = [
        _thread("a", "2026-09-12T10:00:00+00:00"),
        _thread("b", "2026-09-12T08:00:00+00:00"),
        _thread("c", "2026-09-12T06:00:00+00:00"),
    ]
    calls = {"count": 0}

    peers = [FleetPeer(key="mini", name="mini", base_url="http://mini:18080")]
    monkeypatch.setattr(fleet, "owner_access_token", lambda: "token")
    monkeypatch.setattr(fleet, "fleet_peers", lambda: peers)

    def flaky_peer(peer, token, *, page, page_size, cursor):
        calls["count"] += 1
        if calls["count"] > 1:
            return None  # became unreachable after its first page
        return SourcePage(
            items=[
                {
                    "thread_id": "d",
                    "updated_at": "2026-09-12T09:00:00+00:00",
                    fleet.ORIGIN_DEVICE_KEY: peer.name,
                }
            ],
            next_cursor="1",
        )

    monkeypatch.setattr(fleet, "_fetch_peer_thread_page", flaky_peer)

    collected = _drain(2, _paged_source(local, 2))

    assert [item["thread_id"] for item in collected] == ["a", "d", "b", "c"]


def test_garbage_cursor_decodes_to_fresh_start():
    assert decode_fleet_cursor("not-base64!!") is None
    assert decode_fleet_cursor("") is None
    assert decode_fleet_cursor(None) is None


def _report(project_path: str, file_path: str, updated_at: float) -> dict:
    return {
        "id": f"{project_path}:{file_path}",
        "project": {"path": project_path},
        "file": {"path": file_path, "name": file_path.rsplit("/", 1)[-1]},
        "updated_at": updated_at,
    }


def test_fleet_reports_dedupe_across_home_directories(monkeypatch):
    local = [_report("/Users/gabe/Projects/app", "notes/summary.md", 200.0)]
    peer_items = [
        # Same logical report, synced, under a different home root.
        _report("/home/gabe/Projects/app", "notes/summary.md", 150.0),
        # A peer-only report in a project this device does not have.
        _report("/home/gabe/Projects/other", "review.md", 300.0),
    ]

    peers = [FleetPeer(key="mini", name="mini", base_url="http://mini:18080")]
    monkeypatch.setattr(fleet, "owner_access_token", lambda: "token")
    monkeypatch.setattr(fleet, "fleet_peers", lambda: peers)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"items": [dict(item) for item in peer_items]}

    monkeypatch.setattr(fleet, "peer_get", lambda *a, **k: FakeResponse())

    merged = fleet_report_items(local)

    ids = [item["id"] for item in merged]
    assert "/Users/gabe/Projects/app:notes/summary.md" in ids
    assert "/home/gabe/Projects/app:notes/summary.md" not in ids
    assert "/home/gabe/Projects/other:review.md" in ids
    peer_only = next(item for item in merged if "other" in item["id"])
    assert peer_only["origin_device"] == "mini"
    assert peer_only["origin_host"] == "mini"
    assert merged[0]["id"] == "/home/gabe/Projects/other:review.md"  # newest first


def test_peer_thread_page_stamps_origin_host(monkeypatch):
    peer = FleetPeer(
        key="mini.tail1234.ts.net",
        name="mini",
        base_url="http://mini.tail1234.ts.net:18080",
    )

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "threads": [
                    {"thread_id": "t1", "updated_at": "2026-09-12T10:00:00+00:00"}
                ],
                "next": "/api/threads/?page=2&cursor=abc",
            }

    monkeypatch.setattr(fleet, "peer_get", lambda *a, **k: FakeResponse())

    page = fleet._fetch_peer_thread_page(
        peer, "token", page=1, page_size=25, cursor=None
    )

    assert page is not None
    assert page.items[0]["origin_device"] == "mini"
    assert page.items[0]["origin_host"] == "mini.tail1234.ts.net"
    assert page.next_cursor == "abc"


class _FakeFeedResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _install_feed_peer(monkeypatch, payload):
    peers = [FleetPeer(key="mini.ts.net", name="mini", base_url="http://mini.ts.net:18080")]
    monkeypatch.setattr(fleet, "owner_access_token", lambda: "token")
    monkeypatch.setattr(fleet, "fleet_peers", lambda: peers)
    monkeypatch.setattr(fleet, "peer_get", lambda *a, **k: _FakeFeedResponse(payload))


def test_fleet_approvals_concat_stamp_and_sort(monkeypatch):
    _install_feed_peer(
        monkeypatch,
        {"requests": [{"id": "p1", "received_at": "2026-09-12T11:00:00Z"}]},
    )
    local = [{"id": "l1", "received_at": "2026-09-12T10:00:00Z"}]

    merged = fleet.fleet_approval_requests(local)

    assert [item["id"] for item in merged] == ["p1", "l1"]
    assert merged[0]["origin_device"] == "mini"
    assert merged[0]["origin_host"] == "mini.ts.net"
    assert "origin_device" not in merged[1]


def test_fleet_notifications_merges_and_sums_unread(monkeypatch):
    _install_feed_peer(
        monkeypatch,
        {
            "notifications": [
                {"id": "report:x", "created_at": "2026-09-12T11:00:00Z", "read_at": None}
            ],
            "unread_count": 4,
        },
    )
    local = {
        "notifications": [
            {"id": "thread:y", "created_at": "2026-09-12T12:00:00Z", "read_at": None}
        ],
        "unread_count": 2,
    }

    merged = fleet.fleet_notifications(local, include_read=True, limit=10)

    assert [item["id"] for item in merged["notifications"]] == ["thread:y", "report:x"]
    assert merged["unread_count"] == 6
    assert merged["notifications"][1]["origin_host"] == "mini.ts.net"


def test_fleet_notifications_respects_limit(monkeypatch):
    _install_feed_peer(
        monkeypatch,
        {
            "notifications": [
                {"id": f"peer:{i}", "created_at": f"2026-09-12T11:0{i}:00Z"}
                for i in range(5)
            ],
            "unread_count": 5,
        },
    )
    local = {"notifications": [], "unread_count": 0}

    merged = fleet.fleet_notifications(local, include_read=True, limit=3)

    assert len(merged["notifications"]) == 3
    assert merged["unread_count"] == 5


def test_fleet_recent_projects_merges_by_recency(monkeypatch):
    _install_feed_peer(
        monkeypatch,
        {"projects": [{"path": "/home/g/newest", "last_worked_on": "2026-09-12T12:00:00Z"}]},
    )
    local = [{"path": "/Users/g/older", "last_worked_on": "2026-09-12T10:00:00Z"}]

    merged = fleet.fleet_recent_projects(local, page_size=25)

    assert [item["path"] for item in merged] == ["/home/g/newest", "/Users/g/older"]
    assert merged[0]["origin_device"] == "mini"


def test_fleet_feeds_without_token_stay_local(monkeypatch):
    monkeypatch.setattr(fleet, "owner_access_token", lambda: None)

    assert fleet.fleet_approval_requests([{"id": "l1"}]) == [{"id": "l1"}]
    payload = {"notifications": [], "unread_count": 0}
    assert fleet.fleet_notifications(payload, include_read=True, limit=5) == payload
