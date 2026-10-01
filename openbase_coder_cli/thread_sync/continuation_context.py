"""Bounded conversation handoffs, with complete snapshots retained by Openbase."""

from __future__ import annotations

import json

from super_agents.claude_prompts import without_claude_turn_context
from super_agents.initial_context import strip_initial_context
from super_agents.session_history import MAX_HISTORY_BYTES

CONTEXT_CHAR_BUDGET = 64_000


def combine_messages(inherited: list[dict], current: list[dict]) -> list[dict]:
    messages = []
    for message in [*inherited, *current]:
        text = message["text"]
        if message["role"] == "user":
            text = without_claude_turn_context(strip_initial_context(text))
        if text.strip():
            messages.append({**message, "text": text})
    if (
        sum(len(message["text"].encode("utf-8")) for message in messages)
        > MAX_HISTORY_BYTES
    ):
        raise ValueError(
            "Combined conversation history exceeds the 8 MB transfer limit."
        )
    return messages


def build_context(messages: list[dict], source_name: str) -> tuple[str, bool]:
    # Prefer exact text over a lossy generated summary. Preserve early user
    # constraints plus recent exchanges when the full history won't fit.
    serialized = [json.dumps(message, ensure_ascii=False) for message in messages]
    selected: dict[int, str] = {}
    remaining = CONTEXT_CHAR_BUDGET
    early = [i for i, item in enumerate(messages) if item["role"] == "user"][:5]
    for i in [*early, *reversed(range(len(messages)))]:
        if i in selected:
            continue
        text = serialized[i]
        if len(text) <= remaining:
            selected[i] = text
            remaining -= len(text) + 1
    omitted = len(selected) != len(messages)
    note = (
        "Some older or oversized messages are omitted from this handoff. "
        "The complete available snapshot can be read from the continuation context API; "
        "ask the user for details if needed."
        if omitted
        else "All available exported messages are included."
    )
    return (
        f"Previous conversation: {source_name}\n{note}\n"
        "Messages below are chronological JSON records with their original roles.\n"
        + "\n".join(selected[i] for i in sorted(selected)),
        omitted,
    )
