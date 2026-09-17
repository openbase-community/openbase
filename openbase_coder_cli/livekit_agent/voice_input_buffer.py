"""Retain speech fragments cancelled before the backend receives the user turn."""
from dataclasses import dataclass
import time
import re

from openbase_coder_cli.livekit_agent.text_normalization import normalize_spoken_text


@dataclass(frozen=True)
class BufferedVoiceInput:
    prompt: str
    route: object
    revision: int


class VoiceInputBuffer:
    def __init__(self, *, ttl_seconds: float = 30):
        self._ttl_seconds = ttl_seconds
        self._current: BufferedVoiceInput | None = None
        self._updated_at = 0.0
        self._revision = 0

    def add(self, prompt: str, route: object) -> BufferedVoiceInput:
        previous = self._current
        if previous and previous.route == route and time.monotonic() - self._updated_at <= self._ttl_seconds:
            old, new = normalize_spoken_text(previous.prompt), normalize_spoken_text(prompt)
            if old == new or old.startswith(new + " ") or old.endswith(" " + new):
                prompt = previous.prompt
            elif not new.startswith(old + " "):
                old_tokens, new_tokens = old.split(), new.split()
                overlap = next((size for size in range(min(len(old_tokens), len(new_tokens)), 1, -1)
                    if old_tokens[-size:] == new_tokens[:size]), 0)
                if overlap:
                    # LiveKit may replay a cumulative tail with one new clause.
                    # Keep only that clause, preserving its original wording.
                    spans = list(re.finditer(r"\w+(?:['’ʼ]\w+)*", prompt))
                    prompt = previous.prompt + " " + prompt[spans[overlap].start():] if overlap < len(spans) else previous.prompt
                else:
                    prompt = previous.prompt + " " + prompt
        self._revision += 1
        self._current = BufferedVoiceInput(prompt, route, self._revision)
        self._updated_at = time.monotonic()
        return self._current

    def consume(self, item: BufferedVoiceInput, route: object) -> bool:
        if self._current != item or item.route != route:
            return False
        self._current = None
        return True

    def clear(self) -> None:
        self._current = None
