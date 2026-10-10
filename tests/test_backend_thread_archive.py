"""Archive backend sessions without deleting their identities or history."""

from __future__ import annotations

# ruff: noqa: E402, I001
import asyncio
import copy
import os
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django
import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import thread_cache, threads
from openbase_coder_cli.services import fleet_aggregation
from openbase_coder_cli.thread_sync.session_manager import CodexAppServerSessionManager


class BackendSessions:
    def __init__(self, with_history: bool):
        self.rows = {
            key: {
                "id": key,
                "name": key,
                "agentName": key,
                "cwd": "/workspace/project",
                "status": "unknown",
                "createdAt": "2026-10-01T12:00:00Z",
                "updatedAt": "2026-10-01T12:00:00Z",
            }
            for key in ("archived-target", "retained-neighbor")
        }
        self.turns = (
            [
                {
                    "turnId": "retained-turn",
                    "promptPreview": "Preserve history",
                    "status": "completed",
                    "createdAt": "2026-10-01T12:00:01Z",
                    "finishedAt": "2026-10-01T12:00:02Z",
                    "lastUsefulMessage": "Retained reply",
                }
            ]
            if with_history
            else []
        )
        self.cancelled = []

    async def sessions(self):
        return copy.deepcopy(list(self.rows.values()))

    async def read_by_label(self, query, include_turns=False):
        if query.thread_id not in self.rows:
            raise ValueError("Unknown thread")
        return {
            "threadId": query.thread_id,
            "backend": "claude_code",
            "session": copy.deepcopy(self.rows[query.thread_id]),
            "turns": copy.deepcopy(
                self.turns if query.thread_id == "archived-target" else []
            ),
        }

    async def cancel_by_label(self, query):
        self.cancelled.append(query.thread_id)
        return {"cancelled": False}  # Idle sessions, including zero-turn threads.


def manager(client):
    return CodexAppServerSessionManager(
        client=client, execution_backend="claude_code", model_for_role=lambda _: None
    )


def request(method, path, view, **kwargs):
    req = getattr(APIRequestFactory(), method.lower())(path)
    force_authenticate(req, user=SimpleNamespace(is_authenticated=True))
    return view(req, **kwargs)


@pytest.mark.parametrize("with_history", [False, True])
def test_archive_excludes_local_and_fleet_after_restart_preserving_history(
    tmp_path, monkeypatch, with_history
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    client = BackendSessions(with_history)
    before_rows, before_turns = copy.deepcopy(client.rows), copy.deepcopy(client.turns)
    current = manager(client)
    monkeypatch.setattr(threads, "get_session_manager", lambda: current)
    monkeypatch.setattr(threads, "get_livekit_shared_thread_id", lambda: None)
    monkeypatch.setattr(threads, "_refresh_projects_from_threads", lambda _: None)
    monkeypatch.setattr(fleet_aggregation, "owner_access_token", lambda: None)
    thread_cache.clear_thread_cache()
    try:
        # Populate both the API cache and the manager's shared backend scan.
        for scope in ("local", "fleet"):
            response = request(
                "GET", f"/api/threads/?scope={scope}", threads.thread_list
            )
            assert response.status_code == 200
            assert {row["thread_id"] for row in response.data["threads"]} == set(
                client.rows
            )
        before = asyncio.run(current.get_thread_state("archived-target"))
        response = request(
            "DELETE",
            "/api/threads/archived-target/",
            threads.thread_detail,
            thread_id="archived-target",
        )
        assert response.status_code == 200
        # No clock advance: a successful archive must not serve the old list floor.
        for restart in (False, True):
            if restart:
                current = manager(client)
                thread_cache.clear_thread_cache()
            for scope in ("local", "fleet"):
                response = request(
                    "GET", f"/api/threads/?scope={scope}", threads.thread_list
                )
                assert response.status_code == 200
                assert [row["thread_id"] for row in response.data["threads"]] == [
                    "retained-neighbor"
                ]
                assert response.data["count"] == 1
            assert [row.session_id for row in asyncio.run(current.list_threads())] == [
                "retained-neighbor"
            ]
            after = asyncio.run(current.get_thread_state("archived-target"))
            assert after.model_dump() == before.model_dump()
            history = request(
                "GET",
                "/api/threads/archived-target/",
                threads.thread_detail,
                thread_id="archived-target",
            )
            assert history.status_code == 200
        assert client.rows == before_rows
        assert client.turns == before_turns
        assert client.cancelled == ["archived-target"]
        missing = request(
            "DELETE",
            "/api/threads/unknown/",
            threads.thread_detail,
            thread_id="unknown",
        )
        assert missing.status_code == 404
    finally:
        thread_cache.clear_thread_cache()


def test_archive_filters_other_manager_warm_cache_and_keeps_backend_identity(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    client = BackendSessions(False)
    writer, reader = manager(client), manager(client)
    assert len(asyncio.run(reader.list_threads())) == 2
    assert asyncio.run(writer.archive_thread("archived-target"))
    assert [row.session_id for row in asyncio.run(reader.list_threads())] == [
        "retained-neighbor"
    ]
    # A backend with a coincidentally identical thread ID owns a different identity.
    other = CodexAppServerSessionManager(
        client=client, execution_backend="other-backend", model_for_role=lambda _: None
    )
    assert len(asyncio.run(other.list_threads())) == 2
    assert asyncio.run(writer.archive_thread("archived-target"))  # Idempotent.


def test_archive_store_refuses_unknown_schema_and_preserves_marker(
    tmp_path, monkeypatch
):
    import sqlite3
    from openbase_coder_cli.thread_archives import (
        ARCHIVES_FILE,
        archive_backend_thread,
        archived_thread_ids,
    )

    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    archive_backend_thread("claude_code", "retained-history")
    assert archived_thread_ids("claude_code") == {"retained-history"}
    with sqlite3.connect(tmp_path / ARCHIVES_FILE) as connection:
        connection.execute("UPDATE archive_metadata SET schema_version = 99")
    with pytest.raises(RuntimeError, match="Unsupported thread archive schema"):
        archived_thread_ids("claude_code")
    with pytest.raises(RuntimeError, match="Unsupported thread archive schema"):
        archive_backend_thread("claude_code", "new-id")
    with sqlite3.connect(tmp_path / ARCHIVES_FILE) as connection:
        assert connection.execute(
            "SELECT thread_id FROM archived_threads"
        ).fetchall() == [("retained-history",)]


def test_archive_storage_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    from openbase_coder_cli.thread_sync import session_manager_threads

    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    current = manager(BackendSessions(False))

    def cannot_persist(*_):
        raise OSError("Archive storage unavailable")

    monkeypatch.setattr(session_manager_threads, "archive_backend_thread", cannot_persist)
    with pytest.raises(OSError, match="Archive storage unavailable"):
        asyncio.run(current.archive_thread("archived-target"))
    assert len(asyncio.run(current.list_threads())) == 2


def test_codex_archive_keeps_native_backend_operation(tmp_path, monkeypatch):
    from openbase_coder_cli.thread_archives import ARCHIVES_FILE

    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))

    class CodexClient:
        calls = []

        async def read_thread(self, thread_id, include_turns):
            return {}  # No active turn to interrupt.

        async def ensure_connected(self):
            pass

        async def request(self, method, payload):
            self.calls.append((method, payload))
            return {}

    client = CodexClient()
    current = CodexAppServerSessionManager(
        client=client, execution_backend="codex", model_for_role=lambda _: None
    )
    assert asyncio.run(current.archive_thread("codex-thread"))
    assert client.calls == [("thread/archive", {"threadId": "codex-thread"})]
    assert not (tmp_path / ARCHIVES_FILE).exists()
