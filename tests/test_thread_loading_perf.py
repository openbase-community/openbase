"""Thread-loading cost controls.

Thread lists and the console's thread-open burst were paying for the same
small state files hundreds of times per request and for a full report rescan
per notification poll; these pin the caches that keep those costs flat.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402

django.setup()

from openbase_coder_cli import reports_service  # noqa: E402
from openbase_coder_cli.json_snapshot import JsonFileSnapshot  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    item_tags,
    thread_metadata,
)
from openbase_coder_cli.services import fleet_aggregation as fleet  # noqa: E402


def test_json_snapshot_parses_once_per_file_version(tmp_path: Path) -> None:
    parses: list[object] = []
    snapshot: JsonFileSnapshot[dict] = JsonFileSnapshot(
        lambda raw: parses.append(raw) or (raw if isinstance(raw, dict) else {})
    )
    path = tmp_path / "state.json"

    assert snapshot.get(path) == {}
    assert snapshot.get(path) == {}
    assert parses == [None], "a missing file is parsed once, not stat-thrashed"

    path.write_text(json.dumps({"a": 1}))
    assert snapshot.get(path) == {"a": 1}
    assert snapshot.get(path) == {"a": 1}
    assert len(parses) == 2

    # An atomic replace (new inode) is picked up on the next read.
    tmp = tmp_path / "state.json.tmp"
    tmp.write_text(json.dumps({"a": 2}))
    os.replace(tmp, path)
    assert snapshot.get(path) == {"a": 2}

    # read_fresh bypasses the cache; invalidate drops the entry.
    assert snapshot.read_fresh(path) == {"a": 2}
    assert snapshot.read_fresh(path) is not snapshot.get(path)
    snapshot.invalidate(path)
    snapshot.get(path)
    assert len(parses) == 6


def test_tag_labels_skip_catalog_and_serve_from_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    item_tags.set_thread_tags("thread-1", ["Client Work"])
    item_tags.set_report_tags(str(tmp_path), "notes/a.md", ["Needs Review"])

    parses: list[object] = []
    original = item_tags._parse_tags_payload

    def counting(raw):
        parses.append(raw)
        return original(raw)

    monkeypatch.setattr(item_tags._tags_snapshot, "_parse", counting)
    item_tags._tags_snapshot.invalidate()

    for _ in range(50):
        assert item_tags.thread_tags("thread-1") == ["Client Work"]
        assert item_tags.report_tags(str(tmp_path), "notes/a.md") == ["Needs Review"]
        assert item_tags.thread_tags("unknown") == []
    assert len(parses) == 1, "unchanged tags file is parsed once, not per item"

    # A write through the module is visible immediately.
    item_tags.set_thread_tags("thread-1", ["Client Work", "Urgent"])
    assert item_tags.thread_tags("thread-1") == ["Client Work", "Urgent"]
    # The API payload shape is unchanged.
    payload = item_tags.thread_tags_payload("thread-1")
    assert payload["tags"] == ["Client Work", "Urgent"]
    assert {opt["slug"] for opt in payload["tag_options"]} == {
        "client-work",
        "urgent",
        "needs-review",
    }


def test_report_file_payload_reuses_titles_but_refreshes_tags(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path / "data"))
    reports_service._FILE_PAYLOAD_CACHE.clear()
    project = tmp_path / "project"
    reports_dir = project / ".reports"
    reports_dir.mkdir(parents=True)
    report = reports_dir / "summary.md"
    report.write_text("# First title\n\nbody\n")

    title_reads: list[Path] = []
    original = reports_service._report_markdown_title
    monkeypatch.setattr(
        reports_service,
        "_report_markdown_title",
        lambda path: title_reads.append(path) or original(path),
    )

    first = reports_service._list_reports_files(str(project))
    second = reports_service._list_reports_files(str(project))
    assert first[0]["title"] == "First title"
    assert second[0]["title"] == "First title"
    assert len(title_reads) == 1, "an unchanged report is not re-read per listing"

    item_tags.set_report_tags(str(project), "summary.md", ["Reviewed"])
    assert reports_service._list_reports_files(str(project))[0]["tags"] == ["Reviewed"]
    assert len(title_reads) == 1, "a tag change does not invalidate the file payload"

    time.sleep(0.01)
    report.write_text("# Second title\n\nlonger body\n")
    assert (
        reports_service._list_reports_files(str(project))[0]["title"] == "Second title"
    )
    assert len(title_reads) == 2


def test_reports_summary_is_stat_only(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    reports_dir = project / ".reports" / "nested"
    reports_dir.mkdir(parents=True)
    (reports_dir / "a.md").write_text("# A\n")
    (project / ".reports" / "b.txt").write_text("b\n")
    monkeypatch.setattr(
        reports_service,
        "_reports_file_payload",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("summary must not build payloads")
        ),
    )
    summary = reports_service._reports_summary(str(project))
    assert summary["reports_count"] == 2
    assert summary["reports_updated_at"] == max(
        (reports_dir / "a.md").stat().st_mtime,
        (project / ".reports" / "b.txt").stat().st_mtime,
    )
    assert reports_service._reports_summary(str(tmp_path / "missing")) == {
        "reports_count": 0,
        "reports_updated_at": None,
    }


def test_super_agents_agent_name_parses_state_once_per_version(
    tmp_path: Path, monkeypatch
) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setenv("SUPER_AGENTS_STATE_FILE", str(state_path))
    thread_metadata._state_agent_names.invalidate()
    assert thread_metadata._super_agents_agent_name("s1") is None

    state_path.write_text(
        json.dumps(
            {
                "sessions": {
                    "s1": {"threadId": "s1", "name": "one", "agentName": "Alice"},
                    "s2": {"threadId": "s2", "name": "two"},
                }
            }
        )
    )
    parses: list[object] = []
    original = thread_metadata._parse_state_agent_names
    monkeypatch.setattr(
        thread_metadata._state_agent_names,
        "_parse",
        lambda raw: parses.append(raw) or original(raw),
    )
    thread_metadata._state_agent_names.invalidate()
    for _ in range(20):
        assert thread_metadata._super_agents_agent_name("s1") == "Alice"
        assert thread_metadata._super_agents_agent_name("s2") is None
        assert thread_metadata._super_agents_agent_name("missing") is None
    assert len(parses) == 1


def _peer() -> fleet.FleetPeer:
    return fleet.FleetPeer(
        key="mini.ts.net", name="mini", base_url="http://mini.ts.net:18080"
    )


class _Response:
    status_code = 200

    def __init__(self, thread_id: str) -> None:
        self._thread_id = thread_id

    def json(self):
        return {
            "threads": [
                {
                    "thread_id": self._thread_id,
                    "updated_at": "2026-09-12T10:00:00+00:00",
                }
            ],
            "next": None,
        }


def test_peer_thread_pages_serve_stale_while_revalidating(monkeypatch) -> None:
    fleet._peer_page_cache.clear()
    fleet._peer_page_refreshing.clear()
    calls: list[str] = []
    responses = iter(["t1", "t2", "t3"])
    monkeypatch.setattr(
        fleet,
        "peer_get",
        lambda *a, **k: calls.append("get") or _Response(next(responses)),
    )
    now = [1000.0]
    monkeypatch.setattr(fleet.time, "monotonic", lambda: now[0])
    refreshes: list[object] = []

    class ImmediateThread:
        def __init__(self, target, args=(), **kwargs):
            self._target, self._args = target, args
            refreshes.append(args)

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(fleet.threading, "Thread", ImmediateThread)

    first = fleet._fetch_peer_thread_page(
        _peer(), "tok", page=1, page_size=25, cursor=None
    )
    assert first is not None and first.items[0]["thread_id"] == "t1"
    assert calls == ["get"]

    # Fresh window: no peer traffic.
    now[0] += fleet.PEER_PAGE_FRESH_SECONDS - 1
    again = fleet._fetch_peer_thread_page(
        _peer(), "tok", page=1, page_size=25, cursor=None
    )
    assert again is not None and again.items[0]["thread_id"] == "t1"
    assert calls == ["get"] and refreshes == []

    # Stale window: the cached page is served and one refresh runs.
    now[0] += 2
    stale = fleet._fetch_peer_thread_page(
        _peer(), "tok", page=1, page_size=25, cursor=None
    )
    assert stale is not None and stale.items[0]["thread_id"] == "t1"
    assert calls == ["get", "get"] and len(refreshes) == 1
    refreshed = fleet._fetch_peer_thread_page(
        _peer(), "tok", page=1, page_size=25, cursor=None
    )
    assert refreshed is not None and refreshed.items[0]["thread_id"] == "t2"

    # Beyond the stale window the fetch blocks on the peer again.
    now[0] += fleet.PEER_PAGE_STALE_SECONDS + 1
    expired = fleet._fetch_peer_thread_page(
        _peer(), "tok", page=1, page_size=25, cursor=None
    )
    assert expired is not None and expired.items[0]["thread_id"] == "t3"
    assert calls == ["get", "get", "get"]

    # Served copies are independent of the cache (the merge mutates them).
    expired.items[0]["mutated"] = True
    cached_items = fleet._peer_page_cache[("mini.ts.net", 1, 25, None)][1].items
    assert "mutated" not in cached_items[0]
