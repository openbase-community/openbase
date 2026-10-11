"""Sample all thread stacks in-process and log the hottest ones."""

from __future__ import annotations

import sys
import threading
import time
from collections import Counter
from pathlib import Path

DEFAULT_DURATION_SECONDS = 15.0
DEFAULT_INTERVAL_SECONDS = 0.02
MAX_FRAMES = 12
TOP_STACKS = 25
_IDLE_LEAVES = (
    "in wait",
    "in select",
    "in _worker",
    "in sleep",
    "in get",
    "in run_forever",
    "in _run_once",
    "in readinto",
    "in accept",
)


def _stack_key(frame) -> str:
    lines: list[str] = []
    while frame is not None and len(lines) < MAX_FRAMES:
        code = frame.f_code
        lines.append(f"{code.co_filename}:{frame.f_lineno} in {code.co_name}")
        frame = frame.f_back
    return "\n    ".join(lines)


def sample_stacks(
    duration: float = DEFAULT_DURATION_SECONDS,
    interval: float = DEFAULT_INTERVAL_SECONDS,
) -> tuple[Counter[str], int]:
    """Count non-idle thread stacks over `duration`, excluding this thread."""
    me = threading.get_ident()
    counts: Counter[str] = Counter()
    samples = 0
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        for ident, frame in sys._current_frames().items():
            if ident == me:
                continue
            key = _stack_key(frame)
            if key.split("\n", 1)[0].endswith(_IDLE_LEAVES):
                continue
            counts[key] += 1
        samples += 1
        time.sleep(interval)
    return counts, samples


def sample_to_log(path: Path | None = None) -> None:
    path = path or Path.home() / ".openbase" / "logs" / "django-cli-stacks.log"
    counts, samples = sample_stacks()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"=== {time.strftime('%Y-%m-%dT%H:%M:%S')} samples={samples}\n")
        for key, count in counts.most_common(TOP_STACKS):
            handle.write(f"{count:6d}  {key}\n")
