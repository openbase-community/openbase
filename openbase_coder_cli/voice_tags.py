"""Transport marker for transcribed speech sent to coding agents."""

from __future__ import annotations

from html import escape

VOICE_TAG_OPEN = "<voice>"
VOICE_TAG_CLOSE = "</voice>"


def wrap_voice_prompt(prompt: str) -> str:
    """Wrap one speech transcript without allowing transcript-controlled tags."""
    return f"{VOICE_TAG_OPEN}{escape(prompt, quote=False)}{VOICE_TAG_CLOSE}"


def prompt_for_display(prompt: str) -> tuple[str, bool]:
    """Return what the user actually said or typed, and whether it was spoken.

    Stored prompts carry transport the user never wrote: injected
    ``[Openbase system note: …]`` blocks, harness ``<system-reminder>`` blocks
    and the ``<voice>`` envelope. Super Agents owns the stripping rules (the
    same ones the phone apps mirror); the API ships the result so every
    client renders one answer instead of re-implementing them.
    """
    from super_agents.claude_prompts import user_prompt_for_display

    text = user_prompt_for_display(prompt)
    # The envelope escapes its transcript, so a spoken turn is exactly one
    # whose re-wrapped display text still appears in the stored prompt.
    spoken = bool(text) and wrap_voice_prompt(text) in prompt
    return text, spoken
