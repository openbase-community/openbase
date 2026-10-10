from pathlib import Path

import pytest

from openbase_coder_cli.livekit_agent.config import (
    DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS as AGENT_BUILTIN_VOICE_INSTRUCTIONS,
)
from openbase_coder_cli.livekit_voice_route import (
    DIRECT_LIVEKIT_BUILTIN_DEVELOPER_INSTRUCTIONS as ROUTE_BUILTIN_VOICE_INSTRUCTIONS,
)


def test_direct_voice_instructions_include_conservative_background_auto_mute() -> None:
    workspace_root = Path(__file__).resolve().parents[2]
    default_voice_instructions = (
        workspace_root / "instructions" / "VOICE_INSTRUCTIONS.md"
    ).read_text(encoding="utf-8")

    for instructions in (
        default_voice_instructions,
        AGENT_BUILTIN_VOICE_INSTRUCTIONS,
        ROUTE_BUILTIN_VOICE_INSTRUCTIONS,
    ):
        assert "openbase-coder user ios mute" in instructions
        assert "clearly appears to be background conversation" in instructions
        assert "not addressing Openbase Coder" in instructions
        assert "Do not auto-mute ambiguous transcripts" in instructions


def test_direct_voice_instructions_forbid_spoken_commit_details() -> None:
    workspace_root = Path(__file__).resolve().parents[2]
    default_voice_instructions = (
        workspace_root / "instructions" / "VOICE_INSTRUCTIONS.md"
    ).read_text(encoding="utf-8")

    for instructions in (
        default_voice_instructions,
        AGENT_BUILTIN_VOICE_INSTRUCTIONS,
        ROUTE_BUILTIN_VOICE_INSTRUCTIONS,
    ):
        assert "Never read commit hashes or commit subjects aloud" in instructions
        assert "summarize the practical branch or deployment state" in instructions


def test_requested_worker_introduction_survives_rendered_default_deduplication(
    monkeypatch,
) -> None:
    # Field regression: a reused worker refused the caller's explicitly
    # requested hello because a developer-level rule forbade every introduction.
    from openbase_coder_cli import instruction_templates
    from openbase_coder_cli.dispatcher_instructions import with_dispatcher_rules

    monkeypatch.setattr(
        instruction_templates, "get_user_address_name", lambda: "Caller"
    )
    monkeypatch.setattr(
        instruction_templates, "get_dangerous_confirmation_phrase", lambda: "Confirm"
    )
    source = (
        Path(__file__).resolve().parents[2]
        / "instructions"
        / "SUPER_AGENT_INSTRUCTIONS.md"
    )
    worker = instruction_templates.render_instruction_template(source.read_text())
    dispatcher = with_dispatcher_rules("Dispatcher policy.")
    assert "Never run an introduction command" not in worker
    assert "An explicit request from the user" in worker
    assert "overrides that default deduplication" in worker
    assert "do not assume an earlier submission was heard" in worker
    assert "including completion notices" in worker
    assert "Do not claim audible delivery without playback evidence" in worker
    assert "Delegate the task, not announcement instructions" in dispatcher


@pytest.mark.parametrize("backend", ["codex", "openbase_cloud"])
@pytest.mark.parametrize("named", [True, False])
def test_task_only_super_agent_gets_own_announcement_policy(
    tmp_path, monkeypatch, backend, named
):
    from super_agents.app_protocol import with_super_agent_identity_instructions
    from super_agents.mcp_server import clean_thread_input, clean_turn_input

    from openbase_coder_cli.codex_home_instructions import (
        ensure_rendered_instruction_file,
    )

    source = (
        Path(__file__).resolve().parents[2]
        / "instructions"
        / "SUPER_AGENT_INSTRUCTIONS.md"
    )
    installed = tmp_path / source.name
    ensure_rendered_instruction_file(source, installed, document_label="worker")
    monkeypatch.setenv("CODEX_SUPER_AGENT_INSTRUCTIONS_PATH", str(installed))
    task = "Read tictactoe.py and tell me what it does. Change no files."
    thread = clean_thread_input(
        {
            "name": "alpha-read",
            "cwd": str(tmp_path),
            **({"agentName": "Carson"} if named else {}),
        }
    )
    turn = clean_turn_input(
        {"threadId": "s_test", "prompt": task, "model": "gpt-fixture-model"},
        backend=backend,
    )
    assert turn["prompt"] == task
    for instructions in (
        thread["developerInstructions"],
        turn["developerInstructions"],
    ):
        effective = with_super_agent_identity_instructions(
            instructions, "alpha-read", "s_test", "Carson" if named else None
        )
        if named:
            assert "Your name is Carson." in effective
        else:
            assert "Super Agent thread name: alpha-read" in effective
            assert 'super-agent-name "<thread name>" --json' in effective
            assert "returned `agent_name`" in effective
        assert (
            "You own your background introduction and completion announcements"
            in effective
        )
        assert "including read-only inspection or research" in effective
        assert "wait for the task's tools to finish" in effective
        assert "Do not call `user say` for an ordinary direct voice reply" in effective
        assert "including completion notices" in effective
        assert "runtime attempts" not in effective


def test_dispatcher_policy_does_not_teach_default_worker_announcements():
    from openbase_coder_cli.dispatcher_instructions import with_dispatcher_rules

    root = Path(__file__).resolve().parents[2]
    for text in (
        with_dispatcher_rules("Dispatcher policy."),
        (
            root / "skills" / "skills" / "openbase-super-agent-dispatcher" / "SKILL.md"
        ).read_text(),
    ):
        assert "Delegate the task, not announcement instructions" in text
        assert "instruct it to\n  announce completion" not in text
        assert "Do not add a text-only" in text
