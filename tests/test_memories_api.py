from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli.openbase_coder_cli_app import views  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app.memories import (  # noqa: E402
    _flatten_project_path,
)


def _request(method: str, path: str, data: dict | None = None):
    factory = APIRequestFactory()
    request = getattr(factory, method)(path, data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def _patch_memory_homes(monkeypatch, codex_home: Path, claude_home: Path) -> None:
    monkeypatch.setattr(views, "CODEX_HOME_DIR", codex_home)
    monkeypatch.setattr(views._memories, "CODEX_HOME_DIR", codex_home)
    monkeypatch.setattr(views, "CLAUDE_CONFIG_DIR", claude_home)
    monkeypatch.setattr(views._memories, "CLAUDE_CONFIG_DIR", claude_home)


def _write_claude_memory(
    claude_home: Path, project_path: str, name: str, content: str
) -> Path:
    memory_dir = claude_home / "projects" / _flatten_project_path(project_path)
    memory_dir = memory_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_file = memory_dir / f"{name}.md"
    memory_file.write_text(content, encoding="utf-8")
    return memory_file


def _seed_codex_db(codex_home: Path, rows: list[tuple]) -> Path:
    db_path = codex_home / "sqlite" / "memories_1.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE stage1_outputs ("
            " thread_id TEXT PRIMARY KEY,"
            " source_updated_at INTEGER NOT NULL,"
            " raw_memory TEXT NOT NULL,"
            " rollout_summary TEXT NOT NULL,"
            " rollout_slug TEXT,"
            " generated_at INTEGER NOT NULL,"
            " usage_count INTEGER,"
            " last_usage INTEGER,"
            " selected_for_phase2 INTEGER NOT NULL DEFAULT 0,"
            " selected_for_phase2_source_updated_at INTEGER)"
        )
        connection.executemany(
            "INSERT INTO stage1_outputs"
            " (thread_id, source_updated_at, raw_memory, rollout_summary,"
            " rollout_slug, generated_at) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()
    return db_path


CLAUDE_MEMORY = """---
name: sample-memory
description: A sample memory about the project
metadata:
  type: project
---

The project uses widgets.
"""


def test_memories_list_covers_codex_and_claude(tmp_path: Path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    claude_home = tmp_path / "claude-home"
    project_path = str(tmp_path / "my-project")
    _write_claude_memory(claude_home, project_path, "sample-memory", CLAUDE_MEMORY)
    _write_claude_memory(claude_home, project_path, "MEMORY", "- index line\n")
    _seed_codex_db(
        codex_home,
        [("thread-1", 1, "Remember the widgets.", "Did widget work", "widgets", 1000)],
    )
    codex_file_dir = codex_home / "memories"
    codex_file_dir.mkdir(parents=True)
    (codex_file_dir / "global-note.md").write_text(
        "---\ndescription: A codex note\n---\nBody\n", encoding="utf-8"
    )

    _patch_memory_homes(monkeypatch, codex_home, claude_home)

    response = views.memories_list(_request("get", "/api/memories/"))

    assert response.status_code == 200
    sections = {section["key"]: section for section in response.data["sections"]}
    flat = _flatten_project_path(project_path)
    assert set(sections) == {"codex", f"claude:{flat}"}

    codex_names = [entry["name"] for entry in sections["codex"]["memories"]]
    assert codex_names == ["global-note", "thread-1"]
    thread_entry = sections["codex"]["memories"][1]
    assert thread_entry["read_only"] is True
    assert thread_entry["description"] == "widgets"

    claude_section = sections[f"claude:{flat}"]
    claude_names = [entry["name"] for entry in claude_section["memories"]]
    assert claude_names == ["MEMORY", "sample-memory"]
    assert claude_section["memories"][0]["is_index"] is True
    assert (
        claude_section["memories"][1]["description"]
        == "A sample memory about the project"
    )
    assert claude_section["memories"][1]["memory_type"] == "project"


def test_memories_list_project_scope(tmp_path: Path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    project_path = str(tmp_path / "my-project")
    memory_file = _write_claude_memory(
        claude_home, project_path, "sample-memory", CLAUDE_MEMORY
    )

    _patch_memory_homes(monkeypatch, tmp_path / "codex-home", claude_home)

    response = views.memories_list(
        _request("get", f"/api/memories/?path={project_path}")
    )

    assert response.status_code == 200
    assert [entry["name"] for entry in response.data["memories"]] == ["sample-memory"]
    assert response.data["memories_dir"] == str(memory_file.parent)


def test_memory_detail_read_update_delete(tmp_path: Path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    project_path = str(tmp_path / "my-project")
    memory_file = _write_claude_memory(
        claude_home, project_path, "sample-memory", CLAUDE_MEMORY
    )
    index_file = _write_claude_memory(
        claude_home,
        project_path,
        "MEMORY",
        "- [Sample](sample-memory.md) — hook\n- [Other](other.md) — hook\n",
    )

    _patch_memory_homes(monkeypatch, tmp_path / "codex-home", claude_home)
    flat = _flatten_project_path(project_path)

    response = views.memory_detail(
        _request("get", f"/api/memories/sample-memory/?scope=claude:{flat}"),
        "sample-memory",
    )
    assert response.status_code == 200
    assert response.data["content"] == CLAUDE_MEMORY
    assert response.data["read_only"] is False

    response = views.memory_detail(
        _request(
            "put",
            f"/api/memories/sample-memory/?path={project_path}",
            {"content": "updated"},
        ),
        "sample-memory",
    )
    assert response.status_code == 200
    assert memory_file.read_text(encoding="utf-8") == "updated"

    response = views.memory_detail(
        _request("delete", f"/api/memories/sample-memory/?path={project_path}"),
        "sample-memory",
    )
    assert response.status_code == 200
    assert not memory_file.exists()
    assert index_file.read_text(encoding="utf-8") == "- [Other](other.md) — hook\n"


def test_codex_thread_memory_is_read_only(tmp_path: Path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    _seed_codex_db(
        codex_home,
        [("thread-1", 1, "Remember the widgets.", "Did widget work", "widgets", 1000)],
    )

    _patch_memory_homes(monkeypatch, codex_home, tmp_path / "claude-home")

    response = views.memory_detail(
        _request("get", "/api/memories/thread-1/?scope=codex"), "thread-1"
    )
    assert response.status_code == 200
    assert "Remember the widgets." in response.data["content"]
    assert "Did widget work" in response.data["content"]
    assert response.data["read_only"] is True

    response = views.memory_detail(
        _request("delete", "/api/memories/thread-1/?scope=codex"), "thread-1"
    )
    assert response.status_code == 405


def test_memory_detail_rejects_traversal(tmp_path: Path, monkeypatch):
    _patch_memory_homes(monkeypatch, tmp_path / "codex-home", tmp_path / "claude-home")

    response = views.memory_detail(
        _request("get", "/api/memories/../secrets/?scope=codex"), "../secrets"
    )
    assert response.status_code == 400

    response = views.memory_detail(
        _request("get", "/api/memories/x/?scope=claude:not/valid"), "x"
    )
    assert response.status_code == 400
