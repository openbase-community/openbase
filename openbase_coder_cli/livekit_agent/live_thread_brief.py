"""Brief a fresh GPT-Live session on the thread it is speaking for.

The agent turn already runs in the existing thread with its full history
(the Dispatcher's persisted thread, or the thread a call started from or
was transferred to). The voice model itself, though, starts each session
blank: instructions and history are fixed at session start, so "what did we
just talk about?" or "the last thing you said" meant nothing to it. A
bounded note of the thread's most recent exchanges, appended as thinking at
session start, gives it that context without changing client delegation.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from openbase_coder_cli.cli.local_server import local_server_url
from openbase_coder_cli.config.local_api_token import get_local_api_token
from openbase_coder_cli.voice_tags import VOICE_TAG_CLOSE, VOICE_TAG_OPEN

logger = logging.getLogger(__name__)
BRIEF_MAX_EXCHANGES = 6
BRIEF_MAX_FIELD_CHARS = 280
BRIEF_MAX_CHARS = 1600
_WHITESPACE_RE = re.compile(r"\s+")
_SYSTEM_NOTE_RE = re.compile(r"\[Openbase system note:.*?\]\s*", re.DOTALL)


@dataclass(frozen=True)
class ThreadExchange:
    caller: str
    answer: str


ThreadExchangeFetcher = Callable[[str], Awaitable[list[ThreadExchange]]]


def _clean(text: str | None) -> str:
    text = _SYSTEM_NOTE_RE.sub("", text or "")
    start, end = text.find(VOICE_TAG_OPEN), text.rfind(VOICE_TAG_CLOSE)
    if start != -1 and end > start:
        text = text[start + len(VOICE_TAG_OPEN) : end]
    return _WHITESPACE_RE.sub(" ", text).strip()


def _bounded(text: str, limit: int = BRIEF_MAX_FIELD_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def exchanges_from_thread_payload(payload: dict) -> list[ThreadExchange]:
    """Completed turns of a thread detail payload, oldest first, text only."""
    turns = list(payload.get("turn_history") or [])
    current = payload.get("current_turn")
    if isinstance(current, dict):
        turns.append(current)
    exchanges = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        caller = _clean(turn.get("prompt"))
        answer = _clean(turn.get("accumulated_output"))
        if caller or answer:
            exchanges.append(ThreadExchange(caller=caller, answer=answer))
    return exchanges


def thread_brief_note(
    exchanges: list[ThreadExchange],
    *,
    agent_label: str,
    max_exchanges: int = BRIEF_MAX_EXCHANGES,
    max_chars: int = BRIEF_MAX_CHARS,
) -> str | None:
    """The thinking note for a session, or None when there is nothing to tell."""
    recent = [e for e in exchanges if e.caller or e.answer][-max_exchanges:]
    if not recent:
        return None
    header = (
        f"Context only, not to be read aloud: the most recent exchanges in "
        f"{agent_label}'s conversation with this caller, oldest first. Use them "
        "when the caller refers to earlier messages; the agent still answers "
        "substantive requests."
    )
    lines = []
    for exchange in recent:
        caller = _bounded(exchange.caller) if exchange.caller else "(no text)"
        answer = _bounded(exchange.answer) if exchange.answer else "(no reply yet)"
        lines.append(f"Caller: {caller} | {agent_label}: {answer}")
    budget = max_chars - len(header) - 1
    kept: list[str] = []
    for line in reversed(lines):
        if sum(len(k) + 1 for k in kept) + len(line) > budget:
            break
        kept.insert(0, line)
    if not kept:
        kept = [_bounded(lines[-1], budget)]
    return header + "\n" + "\n".join(kept)


def fetch_thread_exchanges_sync(thread_id: str) -> list[ThreadExchange]:
    """Read the thread's detail from the local API (the agent runs beside it).

    The server's address comes from the same source the CLI uses: container
    runtimes move it off 7999 (Maritime serves on 18789, 2026-10-10).
    """
    token = get_local_api_token()
    request = urllib.request.Request(
        f"{local_server_url()}/api/threads/{thread_id}/",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return exchanges_from_thread_payload(payload if isinstance(payload, dict) else {})


async def fetch_thread_exchanges(thread_id: str) -> list[ThreadExchange]:
    import asyncio

    return await asyncio.get_running_loop().run_in_executor(
        None, fetch_thread_exchanges_sync, thread_id
    )
