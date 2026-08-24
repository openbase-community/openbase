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
        self.approval_payloads: list[dict] = [{"backend": backend}]

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
        return self.approval_payloads

    async def create_thread(self, directory: str, thread_id: str | None = None):
        thread = _thread(thread_id or f"new-{self.backend}", minutes_ago=0)
        self.threads[thread.session_id] = thread
        return thread

    async def answer_approval_request(self, request_id, decision):
        self.calls.append(("answer_approval_request", str(request_id)))
        return {"answered": True, "backend": self.backend}


class PagingManager(FakeManager):
    async def list_thread_page(self, *, limit: int, cursor: str | None = None):
        from openbase_coder_cli.thread_sync.session_manager_base import ThreadListPage

        ordered = sorted(
            self.threads.values(), key=lambda t: t.updated_at, reverse=True
        )
        start = int(cursor or 0)
        end = start + limit
        return ThreadListPage(
            threads=ordered[start:end],
            next_cursor=str(end) if end < len(ordered) else None,
        )


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


async def test_list_thread_page_merges_and_paginates():
    codex = PagingManager(
        "codex",
        [_thread("c1", minutes_ago=1), _thread("c2", minutes_ago=4)],
    )
    claude = PagingManager(
        "claude_code",
        [_thread("k1", minutes_ago=2), _thread("k2", minutes_ago=3)],
    )
    facade = MultiBackendSessionManager({"codex": codex, "claude_code": claude})

    first = await facade.list_thread_page(limit=3)
    assert [t.session_id for t in first.threads] == ["c1", "k1", "k2"]
    assert first.next_cursor is not None

    second = await facade.list_thread_page(limit=3, cursor=first.next_cursor)
    assert [t.session_id for t in second.threads] == ["c2"]
    assert second.next_cursor is None


async def test_list_thread_page_survives_one_backend_down():
    codex = PagingManager(
        "codex", [_thread("c1", minutes_ago=1), _thread("c2", minutes_ago=2)]
    )
    facade = MultiBackendSessionManager(
        {"codex": codex, "claude_code": ExplodingManager()}
    )
    page = await facade.list_thread_page(limit=5)
    assert [t.session_id for t in page.threads] == ["c1", "c2"]
    assert page.next_cursor is None


async def test_answer_approval_routes_to_owning_backend(managers):
    codex, claude = managers
    claude.approval_payloads = [{"id": "req-7", "backend": "claude_code"}]
    facade = MultiBackendSessionManager({"codex": codex, "claude_code": claude})

    result = await facade.answer_approval_request("req-7", "accept")
    assert result["backend"] == "claude_code"
    assert claude.calls == [("answer_approval_request", "req-7")]

    result = await facade.answer_approval_request("req-unknown", "decline")
    assert result["backend"] == "codex"


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
