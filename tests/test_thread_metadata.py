from __future__ import annotations

import json
from pathlib import Path

from openbase_coder_cli import livekit_voice_route as voice_route
from openbase_coder_cli.livekit_voice_history import (
    get_voice_history_entry_for_agent_name,
    record_voice_assignment,
)
from openbase_coder_cli.openbase_coder_cli_app.thread_metadata import (
    annotate_thread_payload,
)


def test_annotate_thread_payload_includes_active_target_voice_name(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    (tmp_path / "livekit-voice-route.json").write_text(
        json.dumps(
            {
                "dispatcher_thread_id": "dispatcher-1",
                "dispatcher_voice_id": "dispatcher-voice",
                "dispatcher_voice_name": "Jacqueline",
                "active_target_thread_id": "target-1",
                "active_target_kind": "codex_thread",
                "active_target_label": "Target",
                "active_target_voice_id": "voice-1",
                "active_target_voice_name": "Alice",
                "updated_at": 1,
            }
        ),
        encoding="utf-8",
    )

    target = annotate_thread_payload({"thread_id": "target-1", "name": "Target thread"})
    dispatcher = annotate_thread_payload({"thread_id": "dispatcher-1"})
    other = annotate_thread_payload({"thread_id": "other-1"})

    assert dispatcher["voice_route"] == {"role": "dispatcher", "active": False}
    assert dispatcher["display_name"] == "dispatcher"
    assert dispatcher["voice_assignment"] == {
        "thread_id": "dispatcher-1",
        "agent_name": "dispatcher",
        "voice_id": "dispatcher-voice",
        "voice_name": "Jacqueline",
        "source": "route_state",
    }
    assert target["voice_route"] == {"role": "active_target", "active": True}
    assert target["voice_assignment"] == {
        "thread_id": "target-1",
        "agent_name": "Alice",
        "voice_id": "voice-1",
        "voice_name": "Alice",
        "source": "route_state",
    }
    assert target["display_name"] == "Target thread"
    assert other["voice_route"] == {"role": "none", "active": False}
    # Threads outside the voice route still get a stable derived voice so the
    # console can show a consistent human name for them.
    derived_voice = voice_route.super_agent_voice_for_context(
        "other-1", "other-1", None
    )
    assert other["voice_assignment"] == {
        "thread_id": "other-1",
        "agent_name": None,
        "voice_id": derived_voice.voice_id,
        "voice_name": derived_voice.name,
        "source": "derived",
    }


def test_annotate_thread_payload_resolves_missing_voice_name_from_catalog(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    (tmp_path / "livekit-voice-route.json").write_text(
        json.dumps(
            {
                "dispatcher_thread_id": "dispatcher-1",
                "dispatcher_voice_id": "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
                "active_target_thread_id": "target-1",
                "active_target_kind": "codex_thread",
                "active_target_label": "Target",
                "active_target_voice_id": "f786b574-daa5-4673-aa0c-cbe3e8534c02",
                "updated_at": 1,
            }
        ),
        encoding="utf-8",
    )

    target = annotate_thread_payload({"thread_id": "target-1"})
    dispatcher = annotate_thread_payload({"thread_id": "dispatcher-1"})

    assert dispatcher["voice_assignment"]["voice_name"] == "Jacqueline"
    assert target["voice_assignment"]["voice_name"] == "Katie"


def test_annotate_thread_payload_includes_historical_voice(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    record_voice_assignment(
        thread_id="thread-1",
        agent_name="Build Agent",
        cwd="/tmp/project",
        voice_id="voice-1",
        voice_name="Alice",
        kind="codex_thread",
        source="route_transfer",
        seen_at=10,
    )

    payload = annotate_thread_payload(
        {
            "thread_id": "thread-1",
            "name": "Build Agent",
            "directory": "/tmp/project",
        }
    )

    assert payload["voice_route"] == {"role": "none", "active": False}
    assert payload["voice_assignment"] == {
        "thread_id": "thread-1",
        "agent_name": "Build Agent",
        "voice_id": "voice-1",
        "voice_name": "Alice",
        "source": "route_transfer",
    }
    assert payload["display_name"] == "Build Agent"


def test_annotate_thread_payload_includes_favorite_state(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    (tmp_path / "thread-favorites.json").write_text(
        json.dumps(
            {
                "threads": {
                    "thread-1": {
                        "thread_id": "thread-1",
                        "favorited_at": "2026-06-10T12:00:00Z",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    favorite = annotate_thread_payload({"thread_id": "thread-1"})
    ordinary = annotate_thread_payload({"thread_id": "thread-2"})

    assert favorite["is_favorite"] is True
    assert favorite["favorited_at"] == "2026-06-10T12:00:00Z"
    assert ordinary["is_favorite"] is False
    assert ordinary["favorited_at"] is None


def test_annotate_thread_payload_includes_tags(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    from openbase_coder_cli.openbase_coder_cli_app.item_tags import set_thread_tags

    set_thread_tags("thread-1", ["Needs Review"])

    tagged = annotate_thread_payload({"thread_id": "thread-1"})
    ordinary = annotate_thread_payload({"thread_id": "thread-2"})

    assert tagged["tags"] == ["Needs Review"]
    assert ordinary["tags"] == []


def test_annotate_thread_payload_recovers_from_malformed_favorites(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    (tmp_path / "thread-favorites.json").write_text("{", encoding="utf-8")

    payload = annotate_thread_payload({"thread_id": "thread-1"})

    assert payload["is_favorite"] is False
    assert payload["favorited_at"] is None


def test_annotate_thread_payload_does_not_derive_agent_name_from_thread_name(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        voice_route,
        "selected_tts_provider_id",
        lambda: voice_route.CARTESIA_PROVIDER_ID,
    )
    monkeypatch.setattr(
        voice_route,
        "SUPER_AGENT_VOICES",
        (voice_route.CartesiaVoice("voice-a", "Alice"),),
    )
    monkeypatch.setattr(voice_route, "SUPER_AGENT_VOICE_IDS", ("voice-a",))

    payload = annotate_thread_payload(
        {
            "thread_id": "thread-1",
            "name": "Build thread",
            "directory": "/tmp/project",
        }
    )

    # A voice is still derived deterministically, but the agent name is never
    # invented from the thread name.
    assert payload["agent_name"] is None
    assert payload["voice_assignment"] == {
        "thread_id": "thread-1",
        "agent_name": None,
        "voice_id": "voice-a",
        "voice_name": "Alice",
        "source": "derived",
    }
    assert payload["display_name"] == "Build thread"


def test_annotate_thread_payload_prefers_agent_name_voice(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))

    payload = annotate_thread_payload(
        {
            "thread_id": "thread-1",
            "name": "Build Agent",
            "agent_name": "Dottie",
            "directory": "/tmp/project",
        }
    )

    assert payload["voice_assignment"]["thread_id"] == "thread-1"
    assert payload["voice_assignment"]["agent_name"] == "Dottie"
    assert payload["voice_assignment"]["voice_id"]
    assert payload["voice_assignment"]["voice_name"]
    assert payload["voice_assignment"]["source"] == "derived"
    assert payload["display_name"] == "Build Agent"


def test_annotate_thread_payload_uses_super_agents_state_agent_name(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    state_path = tmp_path / "super-agents-state.json"
    monkeypatch.setenv("SUPER_AGENTS_STATE_FILE", str(state_path))
    state_path.write_text(
        json.dumps(
            {
                "sessions": {
                    "thread-1": {
                        "label": "Build Agent",
                        "agentName": "Dottie",
                        "threadId": "thread-1",
                        "updatedAt": "2026-06-17T01:00:00.000Z",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    payload = annotate_thread_payload(
        {
            "thread_id": "thread-1",
            "name": "Build Agent",
            "directory": "/tmp/project",
        }
    )

    assert payload["agent_name"] == "Dottie"
    assert payload["voice_assignment"]["thread_id"] == "thread-1"
    assert payload["voice_assignment"]["agent_name"] == "Dottie"
    assert payload["voice_assignment"]["voice_id"]
    assert payload["voice_assignment"]["voice_name"]
    assert payload["voice_assignment"]["source"] == "derived"


def test_voice_history_resolves_agent_name_to_thread_and_voice(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    record_voice_assignment(
        thread_id="thread-1",
        agent_name="Dottie",
        cwd="/tmp/project",
        voice_id="voice-dottie",
        voice_name="Dottie",
        kind="codex_thread",
        source="test",
        seen_at=10,
    )

    entry = get_voice_history_entry_for_agent_name("  dottie  ")

    assert entry.thread_id == "thread-1"
    assert entry.voice_id == "voice-dottie"


def test_voice_history_selects_latest_matching_agent_name(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    for thread_id, voice_id, seen_at in (
        ("thread-1", "voice-1", 10),
        ("thread-2", "voice-2", 20),
    ):
        record_voice_assignment(
            thread_id=thread_id,
            agent_name="Dottie",
            cwd="/tmp/project",
            voice_id=voice_id,
            voice_name="Dottie",
            kind="codex_thread",
            source="test",
            seen_at=seen_at,
        )

    entry = get_voice_history_entry_for_agent_name("DOTTIE")

    assert entry.thread_id == "thread-2"
    assert entry.voice_id == "voice-2"


def test_voice_history_tie_breaks_matching_agent_name_by_thread_id(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENBASE_CODER_CLI_DATA_DIR", str(tmp_path))
    for thread_id, voice_id in (("thread-1", "voice-1"), ("thread-2", "voice-2")):
        record_voice_assignment(
            thread_id=thread_id,
            agent_name="Dottie",
            cwd="/tmp/project",
            voice_id=voice_id,
            voice_name="Dottie",
            kind="codex_thread",
            source="test",
            seen_at=10,
        )

    entry = get_voice_history_entry_for_agent_name("DOTTIE")

    assert entry.thread_id == "thread-2"
    assert entry.voice_id == "voice-2"
