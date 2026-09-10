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
