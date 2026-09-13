"""Agent memory browsing API views.

Surfaces the persistent memories coding agents accumulate on this machine so
they can be inspected (and pruned) from the console and desktop app:

- Claude Code auto-memory: per-project markdown files under
  ``~/.claude/projects/<flattened-cwd>/memory/`` with a ``MEMORY.md`` index.
- Codex memories: global per-thread memories stored in
  ``memories_1.sqlite`` (``stage1_outputs.raw_memory``), plus any markdown
  files under ``~/.codex/memories/``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.paths import (
    CLAUDE_CONFIG_DIR,
    CODEX_HOME_DIR,
)

CODEX_SCOPE = "codex"
CLAUDE_SCOPE_PREFIX = "claude:"
CLAUDE_PROJECT_DIR_RE = re.compile(r"^[A-Za-z0-9-]+$")
MEMORY_INDEX_FILENAME = "MEMORY.md"
SESSION_LINE_MAX_BYTES = 256 * 1024
CLAUDE_CWD_PROBE_SESSIONS = 3
CLAUDE_CWD_PROBE_LINES = 25


def _claude_projects_dir() -> Path:
    return CLAUDE_CONFIG_DIR / "projects"


def _codex_memories_dir() -> Path:
    return CODEX_HOME_DIR / "memories"


def _codex_memories_db() -> Path | None:
    """Return the Codex memories sqlite database, or None when absent.

    Newer Codex versions keep sqlite databases under ``~/.codex/sqlite/``;
    older ones keep them at the top of ``~/.codex/``.
    """
    for candidate in (
        CODEX_HOME_DIR / "sqlite" / "memories_1.sqlite",
        CODEX_HOME_DIR / "memories_1.sqlite",
    ):
        if candidate.is_file():
            return candidate
    return None


def _flatten_project_path(project_path: str) -> str:
    """Flatten a project path the way Claude Code names its project dirs."""
    resolved = str(Path(project_path).expanduser())
    return re.sub(r"[^A-Za-z0-9]", "-", resolved)


def _claude_memory_dir(project_dir_name: str) -> Path:
    return _claude_projects_dir() / project_dir_name / "memory"


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract description/type from a memory file's YAML-ish frontmatter."""
    result: dict[str, str] = {}
    if not text.startswith("---"):
        return result
    end = text.find("\n---", 3)
    if end == -1:
        return result
    for line in text[3:end].splitlines():
        match = re.match(r"^\s*(name|description|type):\s*(.+?)\s*$", line)
        if match:
            key, value = match.group(1), match.group(2)
            result.setdefault(key, value.strip("\"'"))
    return result


def _iso_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return ""


def _memory_file_entry(memory_file: Path) -> dict[str, str | bool]:
    try:
        text = memory_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    frontmatter = _parse_frontmatter(text)
    entry: dict[str, str | bool] = {
        "name": memory_file.stem,
        "path": str(memory_file),
        "description": frontmatter.get("description", ""),
        "memory_type": frontmatter.get("type", ""),
        "updated_at": _iso_mtime(memory_file),
        "read_only": False,
        "kind": "file",
    }
    if memory_file.name == MEMORY_INDEX_FILENAME:
        entry["is_index"] = True
        entry["description"] = entry["description"] or "Memory index"
    return entry


def _list_memory_files(memory_dir: Path) -> list[dict[str, str | bool]]:
    if not memory_dir.is_dir():
        return []
    entries = [
        _memory_file_entry(child)
        for child in sorted(memory_dir.iterdir())
        if child.is_file() and child.suffix == ".md"
    ]
    # Keep the MEMORY.md index at the top of the list.
    return sorted(entries, key=lambda entry: (not entry.get("is_index"), entry["name"]))


def _claude_project_cwd(project_dir: Path) -> str:
    """Best-effort recovery of a project's real path from its session logs."""
    try:
        sessions = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return ""
    for session_file in sessions[:CLAUDE_CWD_PROBE_SESSIONS]:
        try:
            with session_file.open("r", encoding="utf-8", errors="replace") as handle:
                for _ in range(CLAUDE_CWD_PROBE_LINES):
                    line = handle.readline(SESSION_LINE_MAX_BYTES)
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = record.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
        except OSError:
            continue
    return ""


def _claude_sections() -> list[dict]:
    projects_dir = _claude_projects_dir()
    if not projects_dir.is_dir():
        return []
    sections = []
    for project_dir in sorted(projects_dir.iterdir()):
        memory_dir = project_dir / "memory"
        memories = _list_memory_files(memory_dir)
        if not memories:
            continue
        display_path = _claude_project_cwd(project_dir) or project_dir.name
        sections.append(
            {
                "key": f"{CLAUDE_SCOPE_PREFIX}{project_dir.name}",
                "label": f"Claude Code — {display_path}",
                "agent": "claude",
                "project_path": _claude_project_cwd(project_dir),
                "memories_dir": str(memory_dir),
                "memories": memories,
            }
        )
    sections.sort(
        key=lambda section: max(
            (entry["updated_at"] for entry in section["memories"]), default=""
        ),
        reverse=True,
    )
    return sections


def _epoch_to_iso(value) -> str:
    if not isinstance(value, (int, float)) or value <= 0:
        return ""
    # Codex stores epoch timestamps; tolerate both seconds and milliseconds.
    seconds = value / 1000 if value > 1e12 else value
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _codex_sqlite_rows() -> list[tuple]:
    db_path = _codex_memories_db()
    if db_path is None:
        return []
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return connection.execute(
                "SELECT thread_id, rollout_slug, rollout_summary, generated_at,"
                " raw_memory FROM stage1_outputs"
                " ORDER BY generated_at DESC, thread_id DESC"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return []


def _codex_thread_entry(row: tuple) -> dict[str, str | bool]:
    thread_id, rollout_slug, rollout_summary, generated_at, _raw_memory = row
    summary_first_line = str(rollout_summary or "").strip().splitlines()
    return {
        "name": str(thread_id),
        "path": str(_codex_memories_db() or ""),
        "description": str(rollout_slug or "")
        or (summary_first_line[0] if summary_first_line else ""),
        "memory_type": "thread",
        "updated_at": _epoch_to_iso(generated_at),
        "read_only": True,
        "kind": "codex-thread",
    }


def _codex_file_entries() -> list[dict[str, str | bool]]:
    memories_dir = _codex_memories_dir()
    if not memories_dir.is_dir():
        return []
    entries = []
    for memory_file in sorted(memories_dir.rglob("*.md")):
        if not memory_file.is_file():
            continue
        entry = _memory_file_entry(memory_file)
        entry["name"] = memory_file.relative_to(memories_dir).with_suffix("").as_posix()
        entries.append(entry)
    return entries


def _codex_section() -> dict:
    memories = _codex_file_entries() + [
        _codex_thread_entry(row) for row in _codex_sqlite_rows()
    ]
    return {
        "key": CODEX_SCOPE,
        "label": "Codex memories",
        "agent": "codex",
        "project_path": "",
        "memories_dir": str(_codex_memories_dir()),
        "memories": memories,
    }


def _validate_memory_name(memory_name: str) -> Path:
    relative = Path(memory_name)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"..", ""} for part in relative.parts)
    ):
        raise ValueError("invalid memory name")
    return relative


def _claude_memory_file(memory_dir: Path, memory_name: str) -> Path:
    relative = _validate_memory_name(memory_name)
    if len(relative.parts) != 1:
        raise ValueError("invalid memory name")
    return memory_dir / f"{relative.name}.md"


def _claude_scope_memory_dir(scope: str) -> Path:
    project_dir_name = scope[len(CLAUDE_SCOPE_PREFIX) :]
    if not CLAUDE_PROJECT_DIR_RE.fullmatch(project_dir_name):
        raise ValueError("invalid memory scope")
    return _claude_memory_dir(project_dir_name)


def _remove_index_entry(memory_dir: Path, memory_filename: str) -> None:
    """Drop a deleted memory's pointer line from the MEMORY.md index."""
    index_file = memory_dir / MEMORY_INDEX_FILENAME
    if memory_filename == MEMORY_INDEX_FILENAME or not index_file.is_file():
        return
    try:
        lines = index_file.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [line for line in lines if f"({memory_filename})" not in line]
        if len(kept) != len(lines):
            index_file.write_text("".join(kept), encoding="utf-8")
    except OSError:
        pass


def _codex_thread_content(thread_id: str) -> dict | None:
    for row in _codex_sqlite_rows():
        if str(row[0]) == thread_id:
            entry = _codex_thread_entry(row)
            raw_memory = str(row[4] or "")
            rollout_summary = str(row[2] or "")
            content = raw_memory
            if rollout_summary:
                content = (
                    f"{raw_memory}\n\n---\n\n## Rollout summary\n\n{rollout_summary}"
                    if raw_memory
                    else rollout_summary
                )
            return {"content": content, **entry}
    return None


@api_view(["GET"])
def memories_list(request):
    """List agent memories.

    Query params:
        path: project directory (omit for the global, all-sources view)
    """
    project_path = request.query_params.get("path", "").strip() or None
    if project_path:
        memory_dir = _claude_memory_dir(_flatten_project_path(project_path))
        return Response(
            {
                "memories": _list_memory_files(memory_dir),
                "memories_dir": str(memory_dir),
            }
        )

    sections = [_codex_section(), *_claude_sections()]
    return Response({"sections": sections})


@api_view(["GET", "PUT", "DELETE"])
def memory_detail(request, memory_name):
    """Read, write, or delete a single memory.

    Query params:
        path: project directory (Claude Code project memories)
        scope: "codex" or "claude:<project-dir-name>" (from the list sections)
    """
    project_path = request.query_params.get("path", "").strip() or None
    scope = request.query_params.get("scope", "").strip()

    if not project_path and scope == CODEX_SCOPE:
        return _codex_memory_detail(request, memory_name)

    try:
        if project_path:
            memory_dir = _claude_memory_dir(_flatten_project_path(project_path))
        elif scope.startswith(CLAUDE_SCOPE_PREFIX):
            memory_dir = _claude_scope_memory_dir(scope)
        else:
            return Response(
                {"error": "invalid memory scope"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        memory_file = _claude_memory_file(memory_dir, memory_name)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    if request.method == "DELETE":
        if not memory_file.is_file():
            return Response(
                {"error": f"Memory '{memory_name}' not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        memory_file.unlink()
        _remove_index_entry(memory_dir, memory_file.name)
        return Response({"success": True})

    if request.method == "PUT":
        if not memory_file.is_file():
            return Response(
                {"error": f"Memory '{memory_name}' not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        content = request.data.get("content", "")
        memory_file.write_text(content, encoding="utf-8")
        return Response({"content": content, "path": str(memory_file)})

    if not memory_file.is_file():
        return Response(
            {"error": f"Memory '{memory_name}' not found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    content = memory_file.read_text(encoding="utf-8", errors="replace")
    return Response({"content": content, **_memory_file_entry(memory_file)})


def _codex_memory_detail(request, memory_name):
    try:
        relative = _validate_memory_name(memory_name)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    memory_file = _codex_memories_dir() / relative.with_suffix(".md")

    if memory_file.is_file():
        if request.method == "DELETE":
            memory_file.unlink()
            return Response({"success": True})
        if request.method == "PUT":
            content = request.data.get("content", "")
            memory_file.write_text(content, encoding="utf-8")
            return Response({"content": content, "path": str(memory_file)})
        content = memory_file.read_text(encoding="utf-8", errors="replace")
        entry = _memory_file_entry(memory_file)
        entry["name"] = memory_name
        return Response({"content": content, **entry})

    thread = _codex_thread_content(memory_name)
    if thread is None:
        return Response(
            {"error": f"Memory '{memory_name}' not found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    if request.method != "GET":
        return Response(
            {"error": "Codex thread memories are read-only"},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )
    return Response(thread)
