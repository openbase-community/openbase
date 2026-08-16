from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from openbase_coder_cli.file_lock import LOCK_EX, LOCK_UN, flock


@contextmanager
def thread_state_file_lock(state_path: Path) -> Iterator[None]:
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        flock(lock_file.fileno(), LOCK_EX)
        try:
            yield
        finally:
            flock(lock_file.fileno(), LOCK_UN)


def load_thread_id(state_path: Path | None) -> str | None:
    if state_path is None or not state_path.is_file():
        return None

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    thread_id = payload.get("dispatcher_thread_id") or payload.get("thread_id")
    return thread_id if isinstance(thread_id, str) and thread_id else None


def persist_thread_id(state_path: Path | None, thread_id: str) -> None:
    if state_path is None:
        return
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"dispatcher_thread_id": thread_id}, indent=2) + "\n",
        encoding="utf-8",
    )


def persist_voice_route_state(
    state_path: Path | None,
    *,
    dispatcher_thread_id: str | None,
    dispatcher_voice: dict[str, str],
    active_target_thread_id: str | None,
    active_target_kind: str | None,
    active_target_label: str | None,
    active_target_voice_id: str | None,
    active_target_voice_name: str | None,
) -> None:
    if state_path is None or not dispatcher_thread_id:
        return
    route_path = (
        state_path
        if state_path.name == "livekit-voice-route.json"
        else state_path.with_name("livekit-voice-route.json")
    )
    route_path.parent.mkdir(parents=True, exist_ok=True)
    route_path.write_text(
        json.dumps(
            {
                "dispatcher_thread_id": dispatcher_thread_id,
                "dispatcher_voice_id": dispatcher_voice["id"],
                "dispatcher_voice_name": dispatcher_voice["name"],
                "active_target_thread_id": active_target_thread_id,
                "active_target_kind": active_target_kind,
                "active_target_label": active_target_label,
                "active_target_voice_id": active_target_voice_id,
                "active_target_voice_name": active_target_voice_name,
                "updated_at": time.time(),
                "instruction_override_supported": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        from openbase_coder_cli.livekit_voice_history import record_voice_assignment

        record_voice_assignment(
            thread_id=dispatcher_thread_id,
            agent_name="dispatcher",
            cwd=None,
            voice_id=dispatcher_voice["id"],
            voice_name=dispatcher_voice["name"],
            kind="dispatcher",
            source="route_state",
        )
        if active_target_thread_id:
            record_voice_assignment(
                thread_id=active_target_thread_id,
                agent_name=active_target_voice_name,
                cwd=None,
                voice_id=active_target_voice_id,
                voice_name=active_target_voice_name,
                kind=active_target_kind or "codex_thread",
                source="route_state",
            )
    except Exception:
        pass
