from pathlib import Path

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
    assert "An explicit request from the user or the delegating agent" in worker
    assert "overrides that default deduplication" in worker
    assert "do not refuse it or assume a prior runtime attempt was heard" in worker
    assert "including completion notices" in worker
    assert "Do not claim audible delivery without playback evidence" in worker
    assert "Preserve an explicit request for a named hello" in dispatcher
