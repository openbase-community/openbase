"""Wire commands preserve complete backend text and the provider event cap."""

import json

import pytest

from openbase_coder_cli.livekit_agent.live_delegation import (
    CHARS_PER_TOKEN_ESTIMATE,
    COMMENTARY_MAX_TOKENS,
    estimate_tokens,
)
from openbase_coder_cli.livekit_agent.live_spoken_output import (
    ANSWER_PREFIX,
    answer_commands,
)


@pytest.mark.parametrize(
    "text",
    [
        "The tests passed. The release is awaiting approval.",
        ('Read "this" carefully. ' * 150),
        "語" * 4000,
        ("\t\r\n\x01" * 900),
    ],
)
def test_spoken_commands_keep_json_and_all_text_within_context_limit(text):
    commands = answer_commands(
        text, max_chars=int(COMMENTARY_MAX_TOKENS * CHARS_PER_TOKEN_ESTIMATE)
    )
    assert all(
        estimate_tokens(command) <= COMMENTARY_MAX_TOKENS for command in commands
    )
    payloads = [json.loads(command.removeprefix(ANSWER_PREFIX)) for command in commands]
    assert "".join("".join(payloads).split()) == "".join(text.split())
