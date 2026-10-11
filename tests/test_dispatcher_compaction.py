from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from openbase_coder_cli import dispatcher_compaction as dc


def _store(
    tmp_path: Path,
    *,
    status: str = "completed",
    active: str | None = None,
    finished: str,
    model: str = "haiku",
):
    db = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "create table sessions (id text, status text, active_turn_id text, model text, updated_at text)"
    )
    conn.execute(
        "create table turns (id text, session_id text, finished_at text, updated_at text)"
    )
    conn.execute(
        "insert into sessions values ('s_disp', ?, ?, ?, ?)",
        (status, active, model, finished),
    )
    conn.execute(
        "insert into turns values ('t1', 's_disp', ?, ?)", (finished, finished)
    )
    conn.commit()
    conn.close()
    return db


def _iso(seconds_ago: float) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - seconds_ago)
    )


class FakeManager:
    def __init__(self):
        self.calls = []

    async def compact_thread(self, thread_id, min_context_tokens):
        self.calls.append((thread_id, min_context_tokens))
        return {"compacted": True, "contextTokens": 150_000, "elapsedMs": 30_000}


@pytest.fixture
def route(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    (tmp_path / "livekit-voice-route.json").write_text(
        json.dumps({"dispatcher_thread_id": "s_disp", "updated_at": 1})
    )


@pytest.mark.asyncio
async def test_idle_dispatcher_is_compacted_at_sixty_percent_of_its_window(
    route, tmp_path
):
    """VM2, 2026-10-11 08:01Z: the Dispatcher auto-compacted at the start of
    a relayed voice steer and stalled it 34 s; compact while idle instead."""
    manager = FakeManager()
    db = _store(tmp_path, finished=_iso(120))
    result = await dc.compact_dispatcher_if_due(manager=manager, state_db_path=db)
    assert result["compacted"] is True
    assert manager.calls == [("s_disp", 120_000)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "store_kwargs",
    [
        {"status": "running", "active": "t_live", "finished": "x"},  # a turn is running
        {"finished": "recent"},  # just answered: a follow-up may be on its way
    ],
)
async def test_busy_or_recently_active_dispatcher_is_left_alone(
    route, tmp_path, store_kwargs
):
    if store_kwargs["finished"] in ("x", "recent"):
        store_kwargs = {**store_kwargs, "finished": _iso(5)}
    manager = FakeManager()
    db = _store(tmp_path, **store_kwargs)
    assert await dc.compact_dispatcher_if_due(manager=manager, state_db_path=db) is None
    assert manager.calls == []


@pytest.mark.asyncio
async def test_long_context_models_use_their_larger_window(route, tmp_path):
    manager = FakeManager()
    db = _store(tmp_path, finished=_iso(120), model="claude-opus-5-5[1m]")
    await dc.compact_dispatcher_if_due(manager=manager, state_db_path=db)
    assert manager.calls == [("s_disp", 600_000)]


@pytest.mark.asyncio
async def test_no_dispatcher_or_no_store_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    manager = FakeManager()
    assert (
        await dc.compact_dispatcher_if_due(
            manager=manager, state_db_path=tmp_path / "none.sqlite3"
        )
        is None
    )
    assert manager.calls == []
