"""Prompt users toward the openbase-onboarding skill until it has been read.

The bundled ``openbase-onboarding`` skill instructs the reading agent to
create ``~/.openbase/onboarding-skill-read`` as soon as the skill is read,
even if onboarding is not completed. Until that marker exists, every user
message bound for the dispatcher gets a note appended requiring the agent
to ask the user to complete or skip onboarding; skipping creates the same
marker directly.
"""

from __future__ import annotations

from openbase_coder_cli.paths import ONBOARDING_SKILL_READ_MARKER_PATH

ONBOARDING_REMINDER = (
    "[Openbase system note: onboarding is pending on this machine — the "
    "openbase-onboarding skill has never been read here. To remove this note "
    "from future messages, read and follow the openbase-onboarding skill now; "
    "its first step records that it was read even if the user skips the rest "
    "of onboarding. The user's current request is primary: answer it fully and "
    "correctly before acting on this note. Then use the skill to ask them to "
    "choose between completing onboarding now and skipping it. Never replace "
    "their requested answer with onboarding guidance.]"
)


def onboarding_skill_read() -> bool:
    """Whether the openbase-onboarding skill has been read on this machine."""
    return ONBOARDING_SKILL_READ_MARKER_PATH.exists()


def append_onboarding_reminder(prompt: str) -> str:
    """Add the onboarding reminder before a dispatcher-bound user message."""
    if onboarding_skill_read():
        return prompt
    if ONBOARDING_REMINDER in prompt:
        return prompt
    # Keep the user's actual request last. Small/fast dispatcher models can
    # overweight a trailing system note and answer only the onboarding nudge;
    # placing the secondary reminder first preserves the primary instruction.
    return f"{ONBOARDING_REMINDER}\n\n{prompt}"
