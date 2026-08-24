from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from openbase_coder_cli.backend_config import configured_execution_backends
from openbase_coder_cli.thread_sync.models import ThreadInfo
from openbase_coder_cli.thread_sync.multi_backend import MultiBackendSessionManager


def _thread(
    thread_id: str, *, minutes_ago: int, backend: str | None = None
) -> ThreadInfo:
    now = datetime.now()
    return ThreadInfo(
        session_id=thread_id,
        directory="/tmp/project",
        backend=backend,
        created_at=now - timedelta(minutes=minutes_ago + 1),
        updated_at=now - timedelta(minutes=minutes_ago),
    )


class FakeManager:
    def __init__(self, backend: str, threads: list[ThreadInfo]):
        self.backend = backend
        self.threads = {t.session_id: t for t in threads}
        self.calls: list[tuple[str, str]] = []

    async def list_threads(self) -> list[ThreadInfo]:
        return list(self.threads.values())

    async def get_session_state(self, session_id: str) -> ThreadInfo | None:
        return self.threads.get(session_id)

    async def start_turn(self, thread_id: str, prompt: str) -> str:
        self.calls.append(("start_turn", thread_id))
        return f"turn-{self.backend}"

    async def interrupt_turn(self, thread_id: str) -> bool:
        self.calls.append(("interrupt_turn", thread_id))
        return True

    async def list_approval_requests(self):
        return [{"backend": self.backend}]

    async def create_thread(self, directory: str, thread_id: str | None = None):
        thread = _thread(thread_id or f"new-{self.backend}", minutes_ago=0)
        self.threads[thread.session_id] = thread
        return thread


class ExplodingManager:
    async def list_threads(self):
        raise RuntimeError("backend down")

    async def list_approval_requests(self):
        raise RuntimeError("backend down")

    async def get_session_state(self, session_id: str):
        raise RuntimeError("backend down")


@pytest.fixture
def managers():
    codex = FakeManager(
        "codex", [_thread("c1", minutes_ago=10), _thread("c2", minutes_ago=1)]
    )
    claude = FakeManager("claude_code", [_thread("k1", minutes_ago=5)])
    return codex, claude


async def test_list_threads_merges_tags_and_sorts(managers):
    codex, claude = managers
    facade = MultiBackendSessionManager({"codex": codex, "claude_code": claude})
    threads = await facade.list_threads()
    assert [t.session_id for t in threads] == ["c2", "k1", "c1"]
    assert {t.session_id: t.backend for t in threads} == {
        "c1": "codex",
        "c2": "codex",
        "k1": "claude_code",
    }


async def test_thread_scoped_calls_route_to_owner(managers):
    codex, claude = managers
    facade = MultiBackendSessionManager({"codex": codex, "claude_code": claude})
    turn = await facade.start_turn("k1", "hello")
    assert turn == "turn-claude_code"
    assert claude.calls == [("start_turn", "k1")]
    assert codex.calls == []

    assert await facade.interrupt_turn("c1") is True
    assert codex.calls == [("interrupt_turn", "c1")]


async def test_unknown_thread_falls_back_to_primary(managers):
    codex, claude = managers
    facade = MultiBackendSessionManager({"codex": codex, "claude_code": claude})
    assert await facade.get_thread_state("nope") is None


async def test_create_thread_targets_backend(managers):
    codex, claude = managers
    facade = MultiBackendSessionManager({"codex": codex, "claude_code": claude})
    created = await facade.create_thread("/tmp/project", backend="claude_code")
    assert created.session_id in claude.threads
    assert created.backend == "claude_code"

    default = await facade.create_thread("/tmp/project")
    assert default.session_id in codex.threads


async def test_one_backend_down_keeps_other_listings(managers):
    codex, _ = managers
    facade = MultiBackendSessionManager(
        {"codex": codex, "claude_code": ExplodingManager()}
    )
    threads = await facade.list_threads()
    assert {t.session_id for t in threads} == {"c1", "c2"}
    approvals = await facade.list_approval_requests()
    assert approvals == [{"backend": "codex"}]


def test_configured_execution_backends_single(monkeypatch):
    monkeypatch.delenv("OPENBASE_CODING_BACKENDS", raising=False)
    backends = configured_execution_backends(lambda: "codex")
    assert len(backends) == 1


def test_configured_execution_backends_mixed(monkeypatch):
    monkeypatch.setenv("OPENBASE_CODING_BACKENDS", "claude-code, codex, bogus, codex")
    backends = configured_execution_backends(lambda: "codex")
    assert backends[0] == configured_execution_backends(lambda: "codex")[0]
    assert set(backends) == {"codex", "claude_code"}
    assert len(backends) == 2
