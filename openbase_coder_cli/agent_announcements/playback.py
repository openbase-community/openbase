"""Suppress queued or playing managed speech after cancellation or a steer."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from openbase_coder_cli.cli.utils import get_data_dir

from .ledger import AnnouncementLedger, turn_revision


class SpeechGuard:
    def __init__(self, ledger, message_id, record):
        self.ledger = ledger
        self.message_id = message_id
        self.record = record
        self._store = None

    def current(self):
        from super_agents.agent_store import Store

        if not self.record:
            return False
        record = self.ledger.read("delivery:" + self.message_id)
        if not record or record.get("cancelled"):
            return False
        if self._store is None:
            self._store = Store(Path(record["store_path"]))
        try:
            session = self._store.get_session(record["thread_id"])
            turn = self._store.get_turn(record["turn_id"])
        except KeyError:
            return False
        return (
            turn.status in {"running", "waiting", "completed"}
            and (session.active_turn_id or session.last_turn_id) == turn.id
            and turn_revision(turn) == record["revision"]
        )


async def speech_guard(message_id, *, ledger=None):
    if not message_id or not message_id.startswith("announcer-managed-"):
        return None
    ledger = ledger or AnnouncementLedger(
        get_data_dir() / "agent-announcements.sqlite3"
    )
    record = await asyncio.to_thread(ledger.read, "delivery:" + message_id)
    return SpeechGuard(ledger, message_id, record)


@asynccontextmanager
async def monitor_speech(guard, interrupt):
    async def watch():
        try:
            while True:
                if not await asyncio.to_thread(guard.current):
                    interrupt()
                    return
                await asyncio.sleep(0.2)
        except Exception:
            interrupt()
            raise

    task = asyncio.create_task(watch()) if guard else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
