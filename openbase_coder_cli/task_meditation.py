"""Guided meditation while a long Super Agent task runs.

Flow (see :func:`run_task_meditation`):

1. A named Super Agent thread's first turn fires the Super Agents thread intro
   hook, which runs ``openbase-coder user intro``. That command greets the
   user and detaches ``openbase-coder meditation run`` for the new thread.
2. Jev (TypeSafe AI's System One decision model) estimates how long the task
   will take from the task name and the recent voice conversation: a score
   question over duration buckets plus a yes/no ("noul") question for "longer
   than the threshold". Without a Jev key, a fast Codex model estimates
   instead.
3. Past the threshold (90 seconds by default) the meditation model (Sol at
   medium reasoning by default) writes a short guided meditation grounded in
   that conversation, with ``<pause N seconds>`` markers between phrases.
4. The voice engine the call itself uses (Cartesia through Openbase Cloud or a
   direct key, or local Kokoro) synthesizes each spoken segment with a calm
   catalog voice; ElevenLabs is an alternative engine. The segments are
   stitched with silence into one WAV file, which plays over the active voice
   call through the announcer audio path (``openbase-coder user play``).

Every stage is optional-failure: a missing API key, an unreachable model, or
no active call ends the run with a recorded outcome and never touches the
Super Agent turn that triggered it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TASK_MEDITATION_CONFIG_KEY = "task_meditation"
ENABLED_ENV = "OPENBASE_TASK_MEDITATION_ENABLED"
THRESHOLD_ENV = "OPENBASE_TASK_MEDITATION_THRESHOLD_SECONDS"
JEV_API_KEY_ENV = "JEV_API_KEY"
TYPESAFE_API_KEY_ENV = "TYPESAFE_API_KEY"
JEV_MODEL_ENV = "OPENBASE_TASK_ESTIMATE_JEV_MODEL"
DECISION_PROBABILITY_ENV = "OPENBASE_TASK_MEDITATION_DECISION_PROBABILITY"
ESTIMATOR_MODEL_ENV = "OPENBASE_TASK_ESTIMATE_MODEL"
ESTIMATOR_REASONING_EFFORT_ENV = "OPENBASE_TASK_ESTIMATE_REASONING_EFFORT"
MEDITATION_MODEL_ENV = "OPENBASE_TASK_MEDITATION_MODEL"
MEDITATION_REASONING_EFFORT_ENV = "OPENBASE_TASK_MEDITATION_REASONING_EFFORT"
TTS_ENGINE_ENV = "OPENBASE_TASK_MEDITATION_TTS"
VOICE_ENV = "OPENBASE_TASK_MEDITATION_VOICE"
ELEVENLABS_API_KEY_ENV = "ELEVENLABS_API_KEY"
ELEVENLABS_VOICE_ID_ENV = "ELEVENLABS_MEDITATION_VOICE_ID"
ELEVENLABS_MODEL_ID_ENV = "ELEVENLABS_MEDITATION_MODEL_ID"

DEFAULT_THRESHOLD_SECONDS = 90.0
DEFAULT_JEV_MODEL = "jev-latest"
# Jev's noul answer is a calibrated probability that the task runs past the
# threshold; at or above this the meditation plays.
DEFAULT_DECISION_PROBABILITY = 0.5
JEV_SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"
# Ordered duration buckets for the Jev score question: representative seconds
# plus the level description Jev sees. Expected seconds is the
# probability-weighted mean of the representative values.
DURATION_LEVELS: tuple[tuple[float, str], ...] = (
    (15.0, "Under 30 seconds: a quick answer or lookup"),
    (60.0, "30 to 90 seconds: a one-line fix or a small check"),
    (135.0, "90 seconds to 3 minutes: a one-file change"),
    (270.0, "3 to 6 minutes: a small change with tests"),
    (630.0, "6 to 15 minutes: a multi-file change with tests"),
    (1800.0, "15 to 45 minutes: a feature across several files"),
    (3600.0, "Over 45 minutes: a large migration or refactor"),
)
DEFAULT_ESTIMATOR_MODEL = "gpt-5.5"
DEFAULT_ESTIMATOR_REASONING_EFFORT = "low"
# The app-server wants the provider slug here, not the Openbase "sol" alias:
# a ChatGPT-account Codex rejects the alias with "model is not supported".
DEFAULT_MEDITATION_MODEL = "gpt-6-sol"
DEFAULT_MEDITATION_REASONING_EFFORT = "medium"
TTS_ENGINE_PRODUCT = "product"
TTS_ENGINE_ELEVENLABS = "elevenlabs"
TTS_ENGINES = (TTS_ENGINE_PRODUCT, TTS_ENGINE_ELEVENLABS)
DEFAULT_TTS_ENGINE = TTS_ENGINE_PRODUCT
# Catalog voice (by name or id) for the product engine; Brooke reads evenly
# and sits lower than the dispatcher's default voice.
DEFAULT_MEDITATION_VOICE = "Brooke"
DEFAULT_PRODUCT_TTS_MODEL = "sonic-3"
# ElevenLabs premade "Sarah": calm, even delivery that suits guided practice.
DEFAULT_ELEVENLABS_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"
DEFAULT_ELEVENLABS_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_MAX_PAUSE_SECONDS = 20.0
DEFAULT_PAUSE_SECONDS = 3.0
# Breathing room between spoken segments that the script did not separate
# with an explicit pause; avoids clipped joins between synthesized chunks.
SEGMENT_GAP_SECONDS = 0.35
SAMPLE_RATE = 24_000
SAMPLE_WIDTH_BYTES = 2
MEDITATIONS_DIR_NAME = "meditations"
RUN_LOCK_FILE = ".run-lock.json"
RUN_LOCK_STALE_SECONDS = 20 * 60
LOG_FILE_NAME = "task-meditation.log"
MAX_CONVERSATION_CHARS = 3_500
MAX_CONVERSATION_TURNS = 8
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
ELEVENLABS_OUTPUT_FORMAT = "pcm_24000"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

PAUSE_PATTERN = re.compile(
    r"[<\[(]\s*pause\s*[:=]?\s*(?P<seconds>\d+(?:\.\d+)?)?\s*"
    r"(?:s|sec|secs|second|seconds)?\s*[>\])]",
    re.IGNORECASE,
)
_VOICE_TAG_PATTERN = re.compile(r"</?voice>", re.IGNORECASE)
_CODE_FENCE_PATTERN = re.compile(r"^```[a-zA-Z]*\s*$|^```\s*$", re.MULTILINE)
_ESTIMATE_JSON_PATTERN = re.compile(
    r'"estimated_seconds"\s*:\s*"?(?P<seconds>\d+(?:\.\d+)?)"?'
)
_ESTIMATE_UNIT_PATTERN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE,
)
_ESTIMATE_BARE_PATTERN = re.compile(r"\d+(?:\.\d+)?")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskMeditationSettings:
    enabled: bool = True
    threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS
    decision_probability: float = DEFAULT_DECISION_PROBABILITY
    jev_api_key: str | None = None
    jev_model: str = DEFAULT_JEV_MODEL
    estimator_model: str = DEFAULT_ESTIMATOR_MODEL
    estimator_reasoning_effort: str = DEFAULT_ESTIMATOR_REASONING_EFFORT
    meditation_model: str = DEFAULT_MEDITATION_MODEL
    meditation_reasoning_effort: str = DEFAULT_MEDITATION_REASONING_EFFORT
    tts_engine: str = DEFAULT_TTS_ENGINE
    voice: str = DEFAULT_MEDITATION_VOICE
    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str = DEFAULT_ELEVENLABS_VOICE_ID
    elevenlabs_model_id: str = DEFAULT_ELEVENLABS_MODEL_ID
    max_pause_seconds: float = DEFAULT_MAX_PAUSE_SECONDS
    output_dir: Path = field(default_factory=lambda: _default_output_dir())

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["output_dir"] = str(self.output_dir)
        data["elevenlabs_api_key"] = "set" if self.elevenlabs_api_key else "missing"
        data["jev_api_key"] = "set" if self.jev_api_key else "missing"
        return data


def _default_output_dir() -> Path:
    from openbase_coder_cli.cli.utils import get_data_dir

    return get_data_dir() / MEDITATIONS_DIR_NAME


def _parse_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_VALUES:
            return True
        if lowered in _FALSE_VALUES:
            return False
    return default


def _parse_positive_float(value: object, default: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_probability(value: object, default: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if 0.0 <= parsed <= 1.0 else default


def _parse_choice(value: object, choices: tuple[str, ...], default: str) -> str:
    if isinstance(value, str) and value.strip().lower() in choices:
        return value.strip().lower()
    return default


def _parse_str(value: object, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def load_task_meditation_settings(
    *,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> TaskMeditationSettings:
    """Resolve settings: defaults, then ``dispatcher-config.json``, then env."""
    from openbase_coder_cli.dispatcher_config import read_dispatcher_config

    source = os.environ if env is None else env
    try:
        config = read_dispatcher_config(config_path).get(TASK_MEDITATION_CONFIG_KEY)
    except ValueError:
        config = None
    config = config if isinstance(config, dict) else {}

    def pick(env_key: str, config_key: str) -> object:
        env_value = source.get(env_key)
        if isinstance(env_value, str) and env_value.strip():
            return env_value
        return config.get(config_key)

    defaults = TaskMeditationSettings()
    return TaskMeditationSettings(
        enabled=_parse_bool(pick(ENABLED_ENV, "enabled"), defaults.enabled),
        threshold_seconds=_parse_positive_float(
            pick(THRESHOLD_ENV, "threshold_seconds"), defaults.threshold_seconds
        ),
        decision_probability=_parse_probability(
            pick(DECISION_PROBABILITY_ENV, "decision_probability"),
            defaults.decision_probability,
        ),
        jev_api_key=(
            _parse_str(source.get(JEV_API_KEY_ENV), "")
            or _parse_str(source.get(TYPESAFE_API_KEY_ENV), "")
            or None
        ),
        jev_model=_parse_str(pick(JEV_MODEL_ENV, "jev_model"), defaults.jev_model),
        estimator_model=_parse_str(
            pick(ESTIMATOR_MODEL_ENV, "estimator_model"), defaults.estimator_model
        ),
        estimator_reasoning_effort=_parse_str(
            pick(ESTIMATOR_REASONING_EFFORT_ENV, "estimator_reasoning_effort"),
            defaults.estimator_reasoning_effort,
        ),
        meditation_model=_parse_str(
            pick(MEDITATION_MODEL_ENV, "meditation_model"), defaults.meditation_model
        ),
        meditation_reasoning_effort=_parse_str(
            pick(MEDITATION_REASONING_EFFORT_ENV, "meditation_reasoning_effort"),
            defaults.meditation_reasoning_effort,
        ),
        tts_engine=_parse_choice(
            pick(TTS_ENGINE_ENV, "tts_engine"), TTS_ENGINES, defaults.tts_engine
        ),
        voice=_parse_str(pick(VOICE_ENV, "voice"), defaults.voice),
        elevenlabs_api_key=_parse_str(source.get(ELEVENLABS_API_KEY_ENV), "") or None,
        elevenlabs_voice_id=_parse_str(
            pick(ELEVENLABS_VOICE_ID_ENV, "voice_id"), defaults.elevenlabs_voice_id
        ),
        elevenlabs_model_id=_parse_str(
            pick(ELEVENLABS_MODEL_ID_ENV, "elevenlabs_model_id"),
            defaults.elevenlabs_model_id,
        ),
        max_pause_seconds=_parse_positive_float(
            config.get("max_pause_seconds"), defaults.max_pause_seconds
        ),
        output_dir=Path(
            _parse_str(config.get("output_dir"), str(_default_output_dir()))
        ).expanduser(),
    )


# --------------------------------------------------------------------------
# Script parsing and audio stitching (pure)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Speech:
    text: str


@dataclass(frozen=True)
class Pause:
    seconds: float


Segment = Speech | Pause


def strip_code_fences(text: str) -> str:
    return _CODE_FENCE_PATTERN.sub("", text).strip()


def parse_meditation_script(
    text: str,
    *,
    max_pause_seconds: float = DEFAULT_MAX_PAUSE_SECONDS,
    default_pause_seconds: float = DEFAULT_PAUSE_SECONDS,
) -> list[Segment]:
    """Split a transcript into spoken segments and pauses.

    Pause markers are tolerant of model drift: ``<pause 5 seconds>``,
    ``<pause 5s>``, ``<pause: 5>``, ``[pause 5 seconds]`` and a bare
    ``<pause>`` (``default_pause_seconds``) all count. Consecutive pauses merge
    and every pause is clamped to ``max_pause_seconds``; leading and trailing
    pauses are dropped because silence before the first word or after the last
    one only delays the call.
    """
    cleaned = strip_code_fences(text)
    segments: list[Segment] = []
    cursor = 0

    def push_speech(chunk: str) -> None:
        spoken = " ".join(chunk.split())
        if spoken:
            segments.append(Speech(spoken))

    def push_pause(seconds: float) -> None:
        clamped = max(0.0, min(seconds, max_pause_seconds))
        if clamped <= 0:
            return
        if segments and isinstance(segments[-1], Pause):
            merged = min(segments[-1].seconds + clamped, max_pause_seconds)
            segments[-1] = Pause(merged)
            return
        segments.append(Pause(clamped))

    for match in PAUSE_PATTERN.finditer(cleaned):
        push_speech(cleaned[cursor : match.start()])
        raw_seconds = match.group("seconds")
        push_pause(float(raw_seconds) if raw_seconds else default_pause_seconds)
        cursor = match.end()
    push_speech(cleaned[cursor:])

    while segments and isinstance(segments[0], Pause):
        segments.pop(0)
    while segments and isinstance(segments[-1], Pause):
        segments.pop()
    return segments


def script_speech_text(segments: Iterable[Segment]) -> str:
    return " ".join(segment.text for segment in segments if isinstance(segment, Speech))


def script_pause_seconds(segments: Iterable[Segment]) -> float:
    return sum(segment.seconds for segment in segments if isinstance(segment, Pause))


def parse_estimate_seconds(text: str) -> float | None:
    """Pull a duration in seconds out of the estimator's reply.

    Prefers the requested JSON field; falls back to the first unit-bearing
    number (``2 minutes``, ``90s``, ``1.5 hours``), then to a bare number read
    as seconds. ``None`` when nothing usable is present.
    """
    if not text:
        return None
    json_match = _ESTIMATE_JSON_PATTERN.search(text)
    if json_match:
        return float(json_match.group("seconds"))
    unit_match = _ESTIMATE_UNIT_PATTERN.search(text)
    if unit_match:
        value = float(unit_match.group("value"))
        unit = unit_match.group("unit").lower()
        if unit.startswith("h"):
            return value * 3600
        if unit.startswith("m"):
            return value * 60
        return value
    bare_match = _ESTIMATE_BARE_PATTERN.search(text)
    if bare_match:
        return float(bare_match.group(0))
    return None


def should_meditate(estimate_seconds: float | None, threshold_seconds: float) -> bool:
    return estimate_seconds is not None and estimate_seconds > threshold_seconds


def silence_pcm(seconds: float, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    frames = max(0, int(round(seconds * sample_rate)))
    return b"\x00" * (frames * SAMPLE_WIDTH_BYTES)


def pcm_duration_seconds(pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> float:
    return len(pcm) / SAMPLE_WIDTH_BYTES / sample_rate


def stitch_pcm(
    rendered: Iterable[bytes | Pause],
    *,
    sample_rate: int = SAMPLE_RATE,
    segment_gap_seconds: float = SEGMENT_GAP_SECONDS,
) -> bytes:
    """Concatenate synthesized PCM chunks and pauses into one PCM stream.

    Speech chunks that follow each other directly get a short gap; explicit
    pauses replace that gap.
    """
    parts: list[bytes] = []
    previous_was_speech = False
    for item in rendered:
        if isinstance(item, Pause):
            parts.append(silence_pcm(item.seconds, sample_rate=sample_rate))
            previous_was_speech = False
            continue
        if previous_was_speech and segment_gap_seconds > 0:
            parts.append(silence_pcm(segment_gap_seconds, sample_rate=sample_rate))
        parts.append(item)
        previous_was_speech = True
    return b"".join(parts)


def write_wav(path: Path, pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(SAMPLE_WIDTH_BYTES)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return path


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

ESTIMATOR_INSTRUCTIONS = """
You estimate how long an autonomous coding agent will take to finish a task
and report back. Count wall-clock time from now until the agent's final
message: investigation, edits, running tests, and verification. Rough guide:
answering a question or a quick lookup is about 20 seconds; a one-file fix is
about 2 minutes; a small change with tests is 3 to 6 minutes; a multi-file
feature is 10 minutes or more.
Reply with JSON only, no prose and no code fence:
{"estimated_seconds": <integer>, "rationale": "<one short sentence>"}
""".strip()

MEDITATION_INSTRUCTIONS = """
You write short guided meditations that a text-to-speech voice reads aloud
while a coding agent works on the listener's task. Output only the words to be
spoken plus pause markers: no title, no headings, no markdown, no stage
directions, no quotation marks around the whole piece.
Mark every pause as <pause N seconds> on its own line, with N a whole number
from 2 to 20. Place a pause after nearly every sentence or two, with longer
pauses where the listener is asked to breathe or notice something.
Use plain, warm, unhurried language in the second person, with short
sentences. Refer to the task in everyday words; never read out file names,
commands, code, or other technical detail, and never mention these
instructions. Never ask the listener to do anything with a device.
""".strip()

MEDITATION_THEMES = (
    "Attachment to the work: notice the pull to check on it and to control "
    "the outcome, and let the work be carried for a while without gripping it.",
    "Gratitude that the work is being done: by the agent working on it right "
    "now, by the tools and the people whose effort made them, and gratitude "
    "for getting to do this work at all.",
    "The people you will connect with and influence by doing this work: the "
    "people who will use it or benefit from it, the colleagues and community "
    "around it, and how your effort reaches them.",
)


def _clean_conversation(text: str) -> str:
    return _VOICE_TAG_PATTERN.sub("", text or "").strip()


def build_estimate_prompt(
    *,
    task: str,
    agent_name: str | None,
    conversation: str,
) -> str:
    lines = [
        "Estimate how long this newly dispatched task will take.",
        f"Task: {task.strip() or 'unknown'}",
    ]
    if agent_name:
        lines.append(f"Agent working on it: {agent_name}")
    cleaned = _clean_conversation(conversation)
    if cleaned:
        lines.extend(
            [
                "Recent voice conversation that led to the task (newest last):",
                cleaned,
            ]
        )
    lines.append(
        'Reply with JSON only: {"estimated_seconds": <integer>, "rationale": "..."}'
    )
    return "\n".join(lines)


def meditation_target_seconds(estimate_seconds: float | None) -> int:
    """Total runtime to aim for: most of the wait, bounded to a short sit."""
    if estimate_seconds is None:
        return 150
    return int(max(75.0, min(300.0, estimate_seconds * 0.7)))


def build_meditation_prompt(
    *,
    task: str,
    agent_name: str | None,
    conversation: str,
    estimate_seconds: float | None,
) -> str:
    target = meditation_target_seconds(estimate_seconds)
    who = agent_name or "the agent"
    lines = [
        "Write a guided meditation for someone whose task was just handed to "
        f"{who}, a coding agent that will work on it for a few minutes.",
        f"Task, in the listener's words: {task.strip() or 'their work'}",
    ]
    if estimate_seconds is not None:
        lines.append(
            f"Expected wait: about {int(round(estimate_seconds / 60)) or 1} minutes."
        )
    lines.append(
        f"Aim for about {target} seconds in total, with roughly forty percent of "
        "that as pauses."
    )
    lines.append("Move through these three themes in order, briefly settling in first:")
    for index, theme in enumerate(MEDITATION_THEMES, start=1):
        lines.append(f"{index}. {theme}")
    lines.append(
        "Close with a gentle return: one breath, eyes opening, and an easy "
        f"readiness to hear from {who} when the work is done."
    )
    cleaned = _clean_conversation(conversation)
    if cleaned:
        lines.extend(
            [
                "Recent conversation, for grounding only (use its spirit, not its "
                "details):",
                cleaned,
            ]
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Recent conversation
# --------------------------------------------------------------------------


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict):
            kind = block.get("type")
            if kind in {"text", "input_text", "output_text"}:
                parts.append(str(block.get("text") or ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(part for part in parts if part)


def conversation_lines_from_thread(payload: Mapping[str, Any]) -> list[str]:
    """Flatten a Super Agents thread read into ``User:``/``Agent:`` lines.

    Tolerates both backends' shapes: Claude turn views carry ``prompt`` plus a
    final message field; Codex turns carry ``items`` with ``userMessage`` and
    ``agentMessage`` entries. Turns are ordered by ``createdAt`` when present.
    """
    turns: Any = payload.get("turns")
    if not isinstance(turns, list):
        thread = payload.get("thread")
        turns = thread.get("turns") if isinstance(thread, dict) else None
    if not isinstance(turns, list):
        turns = payload.get("recentTurns")
    if not isinstance(turns, list):
        return []
    dict_turns = [turn for turn in turns if isinstance(turn, dict)]
    if all(isinstance(turn.get("createdAt"), str) for turn in dict_turns):
        dict_turns.sort(key=lambda turn: str(turn.get("createdAt")))

    lines: list[str] = []
    for turn in dict_turns:
        user_text = ""
        agent_text = ""
        for key in ("prompt", "promptPreview", "input"):
            value = turn.get(key)
            if isinstance(value, str) and value.strip():
                user_text = value
                break
        for key in (
            "finalMessage",
            "lastUsefulMessage",
            "lastAgentMessage",
            "reply",
            "summary",
        ):
            value = turn.get(key)
            if isinstance(value, str) and value.strip():
                agent_text = value
                break
        items = turn.get("items")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "userMessage" and not user_text:
                user_text = _text_from_content(item.get("content") or item.get("text"))
            elif kind == "agentMessage":
                text = str(item.get("text") or "")
                if text.strip():
                    agent_text = text
        user_text = _clean_conversation(user_text)
        agent_text = _clean_conversation(agent_text)
        if user_text:
            lines.append(f"User: {' '.join(user_text.split())}")
        if agent_text:
            lines.append(f"Agent: {' '.join(agent_text.split())}")
    return lines


def trim_conversation(
    lines: list[str], *, max_chars: int = MAX_CONVERSATION_CHARS
) -> str:
    """Keep the newest lines that fit in ``max_chars``, clipping long ones."""
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        clipped = line if len(line) <= 600 else line[:597] + "..."
        if used + len(clipped) + 1 > max_chars:
            break
        kept.append(clipped)
        used += len(clipped) + 1
    return "\n".join(reversed(kept))


async def recent_conversation_text(
    *,
    max_chars: int = MAX_CONVERSATION_CHARS,
    max_turns: int = MAX_CONVERSATION_TURNS,
    exclude_thread_id: str | None = None,
) -> str:
    """The latest voice conversation (active route first, then dispatcher)."""
    try:
        from openbase_coder_cli.livekit_voice_route import get_livekit_voice_route_state

        state = get_livekit_voice_route_state()
    except Exception:
        logger.debug("task_meditation: voice route state unavailable", exc_info=True)
        return ""
    thread_ids: list[str] = []
    for candidate in (state.active_target_thread_id, state.dispatcher_thread_id):
        if candidate and candidate != exclude_thread_id and candidate not in thread_ids:
            thread_ids.append(candidate)
    if not thread_ids:
        return ""

    try:
        from super_agents.app_models import LabelQueryInput
        from super_agents.multi_backend import MultiBackendClient
    except Exception:
        logger.debug("task_meditation: super_agents unavailable", exc_info=True)
        return ""

    client = MultiBackendClient()
    try:
        for thread_id in thread_ids:
            try:
                payload = await client.read_by_label(
                    LabelQueryInput(thread_id=thread_id, max_items=max_turns),
                    include_turns=True,
                )
            except Exception:
                logger.info(
                    "task_meditation stage=conversation_read_failed thread_id=%s",
                    thread_id,
                    exc_info=True,
                )
                continue
            lines = conversation_lines_from_thread(payload)
            if lines:
                return trim_conversation(lines, max_chars=max_chars)
    finally:
        await _close_quietly(client)
    return ""


async def _close_quietly(client: Any) -> None:
    for attribute in ("aclose", "close"):
        closer = getattr(client, attribute, None)
        if callable(closer):
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.debug("task_meditation: client close failed", exc_info=True)
            return


# --------------------------------------------------------------------------
# Jev task-length estimate
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskEstimate:
    """How long the task should take, and how sure we are it is a long one."""

    seconds: float | None
    over_threshold_probability: float | None = None
    confidence: float | None = None
    source: str = "jev"
    model: str | None = None

    def payload(self) -> dict[str, Any]:
        return asdict(self)


DURATION_QUESTION_KEY = "duration"
OVER_THRESHOLD_QUESTION_KEY = "over_threshold"


def build_jev_estimate_request(
    *,
    task: str,
    agent_name: str | None,
    conversation: str,
    threshold_seconds: float,
    model: str = DEFAULT_JEV_MODEL,
) -> dict[str, Any]:
    """The System One request: one score question over duration buckets and one
    noul question for "longer than the threshold"."""
    state: dict[str, Any] = {
        "task": task.strip() or "unknown",
        "agent": (
            f"{agent_name}, an autonomous coding agent working in the repository"
            if agent_name
            else "an autonomous coding agent working in the repository"
        ),
    }
    cleaned = _clean_conversation(conversation)
    if cleaned:
        state["recent_conversation"] = cleaned
    threshold_label = _seconds_label(threshold_seconds)
    return {
        "model": model,
        "state": state,
        "questions": {
            DURATION_QUESTION_KEY: {
                "type": "score",
                "instructions": (
                    "How long will the coding agent take, wall-clock, from now "
                    "until it reports the task finished, counting investigation, "
                    "edits, running tests, and verification?"
                ),
                "criteria": [description for _seconds, description in DURATION_LEVELS],
            },
            OVER_THRESHOLD_QUESTION_KEY: {
                "type": "noul",
                "instructions": (
                    "Finishing this task will take the coding agent more than "
                    f"{threshold_label} of wall-clock time."
                ),
            },
        },
    }


def _seconds_label(seconds: float) -> str:
    if seconds >= 120 and seconds % 60 == 0:
        return f"{int(seconds // 60)} minutes"
    if seconds == 90:
        return "a minute and a half"
    return f"{int(seconds)} seconds"


def parse_jev_estimate(payload: Mapping[str, Any]) -> TaskEstimate:
    """Turn a System One response into a :class:`TaskEstimate`.

    Expected seconds is the probability-weighted mean of the duration levels;
    when per-level probabilities are missing, the (fractional) score is
    interpolated between neighbouring levels instead.
    """
    answers = payload.get("answers")
    answers = answers if isinstance(answers, Mapping) else {}
    duration = answers.get(DURATION_QUESTION_KEY)
    duration = duration if isinstance(duration, Mapping) else {}
    seconds: float | None = None
    probabilities = duration.get("probabilities")
    if isinstance(probabilities, Mapping):
        weighted = 0.0
        total = 0.0
        for key, value in probabilities.items():
            try:
                index = int(str(key))
                probability = float(value)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(DURATION_LEVELS) and probability > 0:
                weighted += probability * DURATION_LEVELS[index][0]
                total += probability
        if total > 0:
            seconds = weighted / total
    if seconds is None:
        try:
            score = float(duration.get("score"))
        except (TypeError, ValueError):
            score = None
        if score is not None:
            seconds = _interpolate_level_seconds(score)
    confidence = duration.get("confidence")
    over = answers.get(OVER_THRESHOLD_QUESTION_KEY)
    over_probability: float | None = None
    if isinstance(over, Mapping):
        try:
            over_probability = float(over.get("noul"))
        except (TypeError, ValueError):
            over_probability = None
    if over_probability is not None and not 0.0 <= over_probability <= 1.0:
        over_probability = None
    return TaskEstimate(
        seconds=round(seconds, 1) if seconds is not None else None,
        over_threshold_probability=over_probability,
        confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
        source="jev",
        model=str(payload.get("model") or "") or None,
    )


def _interpolate_level_seconds(score: float) -> float:
    clamped = max(0.0, min(float(len(DURATION_LEVELS) - 1), score))
    lower = int(clamped)
    upper = min(lower + 1, len(DURATION_LEVELS) - 1)
    fraction = clamped - lower
    return DURATION_LEVELS[lower][0] + fraction * (
        DURATION_LEVELS[upper][0] - DURATION_LEVELS[lower][0]
    )


def estimate_says_long(
    estimate: TaskEstimate,
    *,
    threshold_seconds: float,
    decision_probability: float,
) -> bool:
    """Jev's calibrated yes/no answer decides; the expected duration is the
    fallback when that answer is missing."""
    if estimate.over_threshold_probability is not None:
        return estimate.over_threshold_probability >= decision_probability
    return should_meditate(estimate.seconds, threshold_seconds)


class JevEstimator:
    """Ask Jev (TypeSafe System One) how long the task will take."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_JEV_MODEL,
        threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS,
        timeout_seconds: float = 30.0,
        url: str = JEV_SYSTEM_ONE_URL,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._threshold_seconds = threshold_seconds
        self._timeout_seconds = timeout_seconds
        self._url = url

    def __call__(
        self, *, task: str, agent_name: str | None, conversation: str
    ) -> TaskEstimate:
        import httpx

        request = build_jev_estimate_request(
            task=task,
            agent_name=agent_name,
            conversation=conversation,
            threshold_seconds=self._threshold_seconds,
            model=self._model,
        )
        response = httpx.post(
            self._url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=request,
            timeout=self._timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Jev request failed with HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Jev returned a non-JSON response.") from exc
        if not isinstance(payload, Mapping):
            raise RuntimeError("Jev returned an unexpected response shape.")
        estimate = parse_jev_estimate(payload)
        if estimate.seconds is None and estimate.over_threshold_probability is None:
            raise RuntimeError(
                "Jev answered neither the duration nor the threshold question."
            )
        return estimate


# --------------------------------------------------------------------------
# Model and speech providers
# --------------------------------------------------------------------------

Completer = Callable[..., Awaitable[str]]
Estimator = Callable[..., TaskEstimate]
Synthesizer = Callable[[str], bytes]
Publisher = Callable[[Path], None]
ConversationReader = Callable[[], Awaitable[str]]


class CodexOneShotCompleter:
    """Run a single prompt on a throwaway Codex app-server thread.

    Reuses the voice dispatcher's app-server client so model, effort, and
    endpoint handling match the rest of the runtime; the thread is read-only
    and never persisted as a voice route.
    """

    def __init__(self, *, endpoint: str | None = None, cwd: str | None = None) -> None:
        self._endpoint = endpoint
        self._cwd = cwd or str(Path.home())

    def _resolve_endpoint(self) -> str:
        if self._endpoint:
            return self._endpoint
        from openbase_coder_cli.codex_control_plane import (
            managed_codex_app_server_endpoint,
        )

        return managed_codex_app_server_endpoint().value

    async def __call__(
        self,
        prompt: str,
        *,
        model: str,
        reasoning_effort: str,
        developer_instructions: str,
    ) -> str:
        from openbase_coder_cli.livekit_agent.codex_app_client import (
            CodexAppServerClient,
        )

        effort = reasoning_effort

        class _OneShotClient(CodexAppServerClient):
            def _configured_reasoning_effort(self) -> str | None:
                return effort

            def _speech_text_for_turn(self, active_turn) -> str:  # type: ignore[override]
                messages = active_turn.agent_messages or []
                return messages[-1] if messages else ""

        client = _OneShotClient(
            ws_url=self._resolve_endpoint(),
            cwd=self._cwd,
            developer_instructions=developer_instructions,
            approval_policy="never",
            sandbox="read-only",
            model_name=model,
            persist_thread=False,
        )
        try:
            result = await client.run_turn(prompt)
        finally:
            await client.aclose()
        if str(result.get("status") or "").lower() == "failed":
            raise RuntimeError(f"Codex turn failed: {_turn_error_message(result)}")
        return str(result.get("_livekit_speech_text") or "")


def _turn_error_message(turn: Mapping[str, Any]) -> str:
    error = turn.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    if isinstance(error, str) and error.strip():
        return error.strip()
    return "no error detail from the app-server"


class ProductVoiceSynthesizer:
    """Synthesize with the voice engine the call itself uses.

    Resolves the selected TTS provider (Cartesia via Openbase Cloud, a direct
    Cartesia key, or local Kokoro) exactly as the voice agent does, picks a
    catalog voice by name or id, and streams each segment through the
    provider's LiveKit TTS on a private event loop thread. Output is 24 kHz
    mono 16-bit PCM so it stitches with the pause silence.
    """

    def __init__(
        self,
        *,
        voice: str | None = None,
        provider_id: str | None = None,
        model: str = DEFAULT_PRODUCT_TTS_MODEL,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        from openbase_coder_cli.dispatcher_config import selected_tts_provider_id
        from openbase_coder_cli.tts_providers import get_tts_provider

        self._provider = get_tts_provider(provider_id or selected_tts_provider_id())
        requested = (voice or "").strip() or DEFAULT_MEDITATION_VOICE
        self._voice = (
            self._provider.voice_for_id(requested)
            or self._provider.voice_for_name(requested)
            or self._provider.voice_for_name(DEFAULT_MEDITATION_VOICE)
            or self._provider.default_announcer_voice()
        )
        self._model = model
        self._sample_rate = sample_rate
        _preload_livekit_plugins(self._provider.provider_id)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._tts: Any | None = None

    @property
    def voice_name(self) -> str:
        return self._voice.name

    @property
    def voice_id(self) -> str:
        return self._voice.id

    @property
    def provider_id(self) -> str:
        return self._provider.provider_id

    def describe(self) -> str:
        return f"{self._provider.display_name} voice {self._voice.name}"

    def _credentials(self) -> dict[str, Any]:
        """Mirror the voice agent's TTS wiring for the selected provider."""
        from openbase_coder_cli.tts_providers import (
            CARTESIA_PROVIDER_ID,
            OPENBASE_CLOUD_TTS_PROVIDER_ID,
        )

        provider_id = self._provider.provider_id
        if provider_id == OPENBASE_CLOUD_TTS_PROVIDER_ID:
            from openbase_coder_cli.config.machine_token_manager import (
                MachineTokenManager,
            )
            from openbase_coder_cli.livekit_agent.config import (
                OPENBASE_CLOUD_AUDIO_BASE_URL,
                OPENBASE_CLOUD_AUDIO_CARTESIA_VERSION,
                WEB_BACKEND_URL,
            )

            token = MachineTokenManager(WEB_BACKEND_URL).get_machine_token()
            if not token:
                raise RuntimeError(
                    "Openbase Cloud audio is selected but no machine token is available; "
                    "run `openbase-coder login`."
                )
            return {
                "api_key": token,
                "base_url": f"{OPENBASE_CLOUD_AUDIO_BASE_URL}/cartesia",
                "api_version": OPENBASE_CLOUD_AUDIO_CARTESIA_VERSION,
                "model": self._model,
            }
        if provider_id == CARTESIA_PROVIDER_ID:
            api_key = os.getenv("CARTESIA_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError(
                    "CARTESIA_API_KEY is not set for the Cartesia voice engine."
                )
            return {"api_key": api_key, "model": self._model}
        return {}

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        # LiveKit plugins fetch their aiohttp session from a context variable
        # the agent worker sets per job. ``run_coroutine_threadsafe`` copies
        # the *calling* thread's context into each task, so the session
        # factory is installed here, on the caller's thread, before any task
        # is scheduled; the factory lazily creates one session on the loop.
        from livekit.agents.utils import http_context

        http_context._new_session_ctx()
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=_serve_tts_loop,
            args=(loop,),
            name="openbase-meditation-tts",
            daemon=True,
        )
        thread.start()
        self._loop, self._thread = loop, thread
        return loop

    def _run(self, coroutine):
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    async def _ensure_tts(self) -> Any:
        if self._tts is None:
            self._tts = self._provider.create_livekit_tts(
                voice_id=self._voice.id, **self._credentials()
            )
        return self._tts

    async def _synthesize(self, text: str) -> bytes:
        tts = await self._ensure_tts()
        stream = tts.stream()
        frames: list[Any] = []
        try:
            stream.push_text(text)
            stream.flush()
            stream.end_input()
            async for event in stream:
                frame = getattr(event, "frame", None)
                if frame is not None and frame.samples_per_channel > 0:
                    frames.append(frame)
        finally:
            await stream.aclose()
        return _frames_to_pcm(frames, sample_rate=self._sample_rate)

    def __call__(self, text: str) -> bytes:
        audio = self._run(self._synthesize(text))
        if len(audio) < SAMPLE_WIDTH_BYTES * 100:
            raise RuntimeError(f"{self.describe()} returned no audio for a segment.")
        return audio

    def close(self) -> None:
        loop = self._loop
        if loop is None:
            return

        async def _shutdown() -> None:
            if self._tts is not None:
                closer = getattr(self._tts, "aclose", None)
                if callable(closer):
                    await closer()
                self._tts = None
            from livekit.agents.utils import http_context

            await http_context._close_http_ctx()

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(timeout=10)
        except Exception:
            logger.debug("task_meditation: tts close failed", exc_info=True)
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._loop, self._thread = None, None


def _serve_tts_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _preload_livekit_plugins(provider_id: str) -> None:
    """Import the TTS plugin on the main thread.

    LiveKit registers plugins at import time and refuses to do so from any
    other thread; the synthesizer streams on a worker loop, so the import
    must happen here, while the constructor still runs on the main thread.
    """
    from openbase_coder_cli.tts_providers import (
        CARTESIA_PROVIDER_ID,
        OPENBASE_CLOUD_TTS_PROVIDER_ID,
    )

    if provider_id in {CARTESIA_PROVIDER_ID, OPENBASE_CLOUD_TTS_PROVIDER_ID}:
        try:
            from livekit.plugins import cartesia  # noqa: F401
        except Exception:
            logger.debug(
                "task_meditation: cartesia plugin preload failed", exc_info=True
            )


def _frames_to_pcm(frames: list[Any], *, sample_rate: int) -> bytes:
    """Join LiveKit audio frames into mono 16-bit PCM at ``sample_rate``."""
    parts: list[bytes] = []
    resampler = None
    for frame in frames:
        channels = int(getattr(frame, "num_channels", 1) or 1)
        rate = int(getattr(frame, "sample_rate", sample_rate) or sample_rate)
        if rate == sample_rate and channels == 1:
            parts.append(bytes(frame.data))
            continue
        from livekit import rtc

        if resampler is None:
            resampler = rtc.AudioResampler(
                input_rate=rate, output_rate=sample_rate, num_channels=channels
            )
        for resampled in resampler.push(frame):
            parts.append(_downmix(bytes(resampled.data), channels))
    if resampler is not None:
        for resampled in resampler.flush():
            parts.append(_downmix(bytes(resampled.data), int(frames[-1].num_channels)))
    return b"".join(parts)


def _downmix(data: bytes, channels: int) -> bytes:
    if channels <= 1:
        return data
    import array

    samples = array.array("h", data)
    mono = array.array(
        "h",
        (
            int(sum(samples[i : i + channels]) / channels)
            for i in range(0, len(samples) - len(samples) % channels, channels)
        ),
    )
    return mono.tobytes()


class ElevenLabsSynthesizer:
    """Synthesize one spoken segment to 24 kHz mono 16-bit PCM."""

    def describe(self) -> str:
        return f"ElevenLabs voice {self._voice_id}"

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str = DEFAULT_ELEVENLABS_VOICE_ID,
        model_id: str = DEFAULT_ELEVENLABS_MODEL_ID,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id
        self._timeout_seconds = timeout_seconds

    def __call__(self, text: str) -> bytes:
        import httpx

        response = httpx.post(
            ELEVENLABS_TTS_URL.format(voice_id=self._voice_id),
            params={"output_format": ELEVENLABS_OUTPUT_FORMAT},
            headers={
                "xi-api-key": self._api_key,
                "accept": "application/octet-stream",
                "content-type": "application/json",
            },
            json={
                "text": text,
                "model_id": self._model_id,
                "voice_settings": {
                    "stability": 0.7,
                    "similarity_boost": 0.8,
                    "style": 0.1,
                    "use_speaker_boost": True,
                },
            },
            timeout=self._timeout_seconds,
        )
        if response.status_code >= 400:
            detail = response.text[:300]
            raise RuntimeError(
                f"ElevenLabs text-to-speech failed with HTTP {response.status_code}: {detail}"
            )
        audio = response.content
        if len(audio) < SAMPLE_WIDTH_BYTES * 100:
            raise RuntimeError("ElevenLabs returned no audio for a segment.")
        return audio


def build_synthesizer(settings: TaskMeditationSettings) -> Synthesizer | None:
    """The configured voice engine, or ``None`` when it cannot be used."""
    if settings.tts_engine == TTS_ENGINE_ELEVENLABS:
        if not settings.elevenlabs_api_key:
            return None
        return ElevenLabsSynthesizer(
            api_key=settings.elevenlabs_api_key,
            voice_id=settings.elevenlabs_voice_id,
            model_id=settings.elevenlabs_model_id,
        )
    return ProductVoiceSynthesizer(voice=settings.voice)


def close_synthesizer(synthesizer: Synthesizer | None) -> None:
    closer = getattr(synthesizer, "close", None)
    if callable(closer):
        closer()


def publish_meditation_audio(path: Path, *, room_name: str | None = None) -> None:
    """Play the file in the active call through the local server's play API."""
    from openbase_coder_cli.cli.local_server import local_server_request

    payload: dict[str, str] = {"audio_path": str(path)}
    if room_name:
        payload["room_name"] = room_name
    response = local_server_request(
        "POST",
        "/api/user/play/",
        json=payload,
        ok_statuses=(502,),
        timeout=60,
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        raise RuntimeError(
            str(detail or f"Playback request failed ({response.status_code}).")
        )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


@dataclass
class MeditationOutcome:
    status: str
    reason: str = ""
    thread_id: str = ""
    task: str = ""
    estimate_seconds: float | None = None
    estimate_source: str | None = None
    over_threshold_probability: float | None = None
    threshold_seconds: float | None = None
    script_path: str | None = None
    audio_path: str | None = None
    audio_seconds: float | None = None
    voice: str | None = None
    record_path: str | None = None

    def payload(self) -> dict[str, Any]:
        return asdict(self)


STATUS_SKIPPED = "skipped"
STATUS_PLAYED = "played"
STATUS_RENDERED = "rendered"
STATUS_FAILED = "failed"


def _slug(value: str, *, limit: int = 40) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned[:limit].strip("-") or "task"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire_run_lock(output_dir: Path) -> Path | None:
    """Claim the single meditation slot; ``None`` when another run is live.

    A lock whose process is gone or that is older than
    ``RUN_LOCK_STALE_SECONDS`` is taken over.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / RUN_LOCK_FILE
    try:
        existing = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = None
    if isinstance(existing, dict):
        pid = existing.get("pid")
        started = existing.get("started_at")
        fresh = (
            isinstance(started, (int, float))
            and (time.time() - started) < RUN_LOCK_STALE_SECONDS
        )
        if isinstance(pid, int) and pid != os.getpid() and fresh and _pid_alive(pid):
            return None
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "started_at": time.time()}), encoding="utf-8"
    )
    return lock_path


def release_run_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        lock_path.unlink()
    except OSError:
        pass


async def run_task_meditation(
    *,
    thread_id: str,
    thread_name: str,
    agent_name: str | None,
    settings: TaskMeditationSettings,
    complete: Completer,
    estimate: Estimator | None,
    synthesize: Synthesizer | None,
    publish: Publisher | None,
    read_conversation: ConversationReader,
    task_text: str | None = None,
    force: bool = False,
    play: bool = True,
) -> MeditationOutcome:
    """Estimate the task, and when it is long enough write, render, and play a meditation.

    ``estimate`` is the Jev estimator (sync, run on a thread); ``None`` falls
    back to asking the Codex estimator model for a JSON estimate.
    """
    task = " ".join((task_text or thread_name or "").split())
    outcome = MeditationOutcome(
        status=STATUS_SKIPPED,
        thread_id=thread_id,
        task=task,
        threshold_seconds=settings.threshold_seconds,
    )
    if not settings.enabled and not force:
        outcome.reason = "disabled"
        return outcome

    conversation = ""
    try:
        conversation = await read_conversation()
    except Exception:
        logger.info("task_meditation stage=conversation_unavailable", exc_info=True)

    if force:
        estimate_seconds: float | None = None
        logger.info(
            "task_meditation stage=estimate_skipped reason=force thread_id=%s",
            thread_id,
        )
    else:
        try:
            task_estimate = await _estimate_task(
                estimate=estimate,
                complete=complete,
                settings=settings,
                task=task,
                agent_name=agent_name,
                conversation=conversation,
            )
        except Exception as exc:
            logger.warning(
                "task_meditation stage=estimate_failed thread_id=%s",
                thread_id,
                exc_info=True,
            )
            outcome.status = STATUS_FAILED
            outcome.reason = f"estimate failed: {exc}"
            return outcome
        estimate_seconds = task_estimate.seconds
        outcome.estimate_seconds = estimate_seconds
        outcome.estimate_source = task_estimate.source
        outcome.over_threshold_probability = task_estimate.over_threshold_probability
        logger.info(
            "task_meditation stage=estimated thread_id=%s source=%s model=%s "
            "estimate_seconds=%s over_threshold_probability=%s confidence=%s threshold=%s",
            thread_id,
            task_estimate.source,
            task_estimate.model or "",
            estimate_seconds,
            task_estimate.over_threshold_probability,
            task_estimate.confidence,
            settings.threshold_seconds,
        )
        if (
            estimate_seconds is None
            and task_estimate.over_threshold_probability is None
        ):
            outcome.reason = "estimate unparsable"
            return outcome
        if not estimate_says_long(
            task_estimate,
            threshold_seconds=settings.threshold_seconds,
            decision_probability=settings.decision_probability,
        ):
            outcome.reason = "under threshold"
            return outcome

    try:
        script_text = await complete(
            build_meditation_prompt(
                task=task,
                agent_name=agent_name,
                conversation=conversation,
                estimate_seconds=estimate_seconds,
            ),
            model=settings.meditation_model,
            reasoning_effort=settings.meditation_reasoning_effort,
            developer_instructions=MEDITATION_INSTRUCTIONS,
        )
    except Exception as exc:
        logger.warning(
            "task_meditation stage=script_failed thread_id=%s", thread_id, exc_info=True
        )
        outcome.status = STATUS_FAILED
        outcome.reason = f"meditation script failed: {exc}"
        return outcome

    segments = parse_meditation_script(
        script_text, max_pause_seconds=settings.max_pause_seconds
    )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = settings.output_dir / f"{stamp}-{_slug(task)}"
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    script_path = base.with_suffix(".txt")
    script_path.write_text(strip_code_fences(script_text) + "\n", encoding="utf-8")
    outcome.script_path = str(script_path)
    if not any(isinstance(segment, Speech) for segment in segments):
        outcome.status = STATUS_FAILED
        outcome.reason = "meditation script had no spoken text"
        _write_record(base, outcome)
        return outcome
    logger.info(
        "task_meditation stage=script_ready thread_id=%s segments=%d speech_chars=%d pause_seconds=%.0f",
        thread_id,
        len(segments),
        len(script_speech_text(segments)),
        script_pause_seconds(segments),
    )

    describe = getattr(synthesize, "describe", None)
    outcome.voice = describe() if callable(describe) else None
    if synthesize is None:
        outcome.reason = "no voice engine available; script saved without audio"
        _write_record(base, outcome)
        return outcome

    rendered: list[bytes | Pause] = []
    try:
        for segment in segments:
            if isinstance(segment, Pause):
                rendered.append(segment)
                continue
            rendered.append(await asyncio.to_thread(synthesize, segment.text))
    except Exception as exc:
        logger.warning(
            "task_meditation stage=synthesis_failed thread_id=%s",
            thread_id,
            exc_info=True,
        )
        outcome.status = STATUS_FAILED
        outcome.reason = f"speech synthesis failed: {exc}"
        _write_record(base, outcome)
        return outcome

    pcm = stitch_pcm(rendered)
    audio_path = write_wav(base.with_suffix(".wav"), pcm)
    outcome.audio_path = str(audio_path)
    outcome.audio_seconds = round(pcm_duration_seconds(pcm), 1)
    outcome.status = STATUS_RENDERED
    logger.info(
        "task_meditation stage=audio_ready thread_id=%s audio_path=%s audio_seconds=%.1f",
        thread_id,
        audio_path,
        outcome.audio_seconds,
    )

    if play and publish is not None:
        try:
            await asyncio.to_thread(publish, audio_path)
        except Exception as exc:
            logger.warning(
                "task_meditation stage=playback_failed thread_id=%s",
                thread_id,
                exc_info=True,
            )
            outcome.status = STATUS_FAILED
            outcome.reason = f"playback failed: {exc}"
            _write_record(base, outcome)
            return outcome
        outcome.status = STATUS_PLAYED
    elif not play:
        outcome.reason = "playback disabled"
    _write_record(base, outcome)
    return outcome


async def _estimate_task(
    *,
    estimate: Estimator | None,
    complete: Completer,
    settings: TaskMeditationSettings,
    task: str,
    agent_name: str | None,
    conversation: str,
) -> TaskEstimate:
    if estimate is not None:
        return await asyncio.to_thread(
            estimate, task=task, agent_name=agent_name, conversation=conversation
        )
    logger.info("task_meditation stage=estimate_fallback reason=no_jev_key")
    reply = await complete(
        build_estimate_prompt(
            task=task, agent_name=agent_name, conversation=conversation
        ),
        model=settings.estimator_model,
        reasoning_effort=settings.estimator_reasoning_effort,
        developer_instructions=ESTIMATOR_INSTRUCTIONS,
    )
    return TaskEstimate(
        seconds=parse_estimate_seconds(reply),
        source="codex",
        model=settings.estimator_model,
    )


def _write_record(base: Path, outcome: MeditationOutcome) -> None:
    record_path = base.with_suffix(".json")
    outcome.record_path = str(record_path)
    try:
        record_path.write_text(
            json.dumps(outcome.payload(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        logger.debug(
            "task_meditation: unable to write record %s", record_path, exc_info=True
        )


# --------------------------------------------------------------------------
# Detached worker
# --------------------------------------------------------------------------


def openbase_coder_command() -> str:
    """The ``openbase-coder`` executable next to this interpreter, else on PATH."""
    venv_command = Path(sys.executable).with_name("openbase-coder")
    if venv_command.is_file():
        return str(venv_command)
    return shutil.which("openbase-coder") or "openbase-coder"


def meditation_worker_argv(
    *,
    thread_id: str,
    thread_name: str,
    agent_name: str | None,
    command: str | None = None,
) -> list[str]:
    argv = [
        command or openbase_coder_command(),
        "meditation",
        "run",
        "--thread-id",
        thread_id,
        "--thread-name",
        thread_name,
    ]
    if agent_name:
        argv.extend(["--agent-name", agent_name])
    return argv


def worker_log_path() -> Path:
    from openbase_coder_cli.paths import DEFAULT_LOG_DIR

    return DEFAULT_LOG_DIR / LOG_FILE_NAME


def spawn_meditation_worker(
    argv: list[str],
    *,
    log_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Start the worker detached from the caller; returns its pid.

    The intro hook that calls us is bounded by a short timeout, so the
    estimate and synthesis must outlive this process.
    """
    log_file = log_path or worker_log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "ab") as handle:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=dict(env) if env is not None else None,
        )
    return process.pid
