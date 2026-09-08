"""Transport marker for transcribed speech sent to coding agents."""

from __future__ import annotations

from html import escape

VOICE_TAG_OPEN = "<voice>"
VOICE_TAG_CLOSE = "</voice>"


def wrap_voice_prompt(prompt: str) -> str:
    """Wrap one speech transcript without allowing transcript-controlled tags."""
    return f"{VOICE_TAG_OPEN}{escape(prompt, quote=False)}{VOICE_TAG_CLOSE}"
