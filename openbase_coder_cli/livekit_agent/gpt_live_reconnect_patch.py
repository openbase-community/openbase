"""Drop input audio queued during a GPT-Live outage instead of replaying it.

The plugin keeps every client event in one unbounded send channel. While
the gateway socket is down (connection lost, retry interval, new handshake,
``session.started``), ``push_audio`` keeps queueing ``session.input_audio.append``
events; the next connection then drains them in a burst, faster than real
time. The gateway answers ``input_audio_rate_limit_exceeded`` and the model
hears seconds of stale audio (Maritime, 2026-10-10 22:11Z). ``_reset_for_reconnect``
already discards the plugin's own audio buffer; this patch also discards the
queued audio events, keeping instructions, thinking and commentary appends
in order so the application's context still reaches the new session.

The same burst happens when a session first starts: every character swap
opens a new session, the call keeps pushing caller audio into it while it
waits for ``session.started``, and the drained queue trips the same limit;
that session then stays deaf until the plugin reconnects (Maritime,
2026-10-11 05:42Z: 55 s with no input transcripts after a transfer). Input
audio pushed before ``session.started`` is therefore dropped as well: no
session can be listening to it yet.

Verified against livekit-plugins-openai 1.8.4: ``GPTLiveSession._msg_ch`` is a
``utils.aio.Chan`` drained by ``_send_task`` after ``session.start``.
"""

from __future__ import annotations

import logging
from collections import deque

from livekit.agents.utils.aio.channel import ChanClosed, ChanEmpty

logger = logging.getLogger(__name__)

INPUT_AUDIO_APPEND_TYPE = "session.input_audio.append"
# Caller audio kept from before session.started and sent once it starts.
PRESTART_AUDIO_KEEP_MS = 1500.0


def _frame_ms(frame) -> float:
    duration = getattr(frame, "duration", None)
    if duration is not None:
        return float(duration) * 1000.0
    rate = getattr(frame, "sample_rate", 0) or 0
    samples = getattr(frame, "samples_per_channel", 0) or 0
    return samples * 1000.0 / rate if rate else 0.0


def _is_input_audio(event) -> bool:
    kind = (
        event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
    )
    return kind == INPUT_AUDIO_APPEND_TYPE


def drain_stale_input_audio(channel) -> int:
    """Remove queued input audio events from ``channel``; keep the rest in order."""
    kept = []
    dropped = 0
    while True:
        try:
            event = channel.recv_nowait()
        except (ChanEmpty, ChanClosed):
            break
        if _is_input_audio(event):
            dropped += 1
        else:
            kept.append(event)
    for event in kept:
        try:
            channel.send_nowait(event)
        except ChanClosed:
            break
    return dropped


def install_gpt_live_reconnect_patch() -> bool:
    try:
        from livekit.plugins.openai.realtime import gpt_live_model
    except Exception:  # noqa: BLE001 - the live engine is optional
        logger.debug("GPT-Live plugin unavailable; reconnect patch not installed")
        return False
    session_cls = getattr(gpt_live_model, "GPTLiveSession", None)
    if session_cls is None or getattr(
        session_cls, "_openbase_reconnect_patched", False
    ):
        return session_cls is not None
    original = session_cls._reset_for_reconnect

    def _reset_for_reconnect(self):
        channel = getattr(self, "_msg_ch", None)
        dropped = drain_stale_input_audio(channel) if channel is not None else 0
        if dropped:
            logger.info(
                "dispatch_timing stage=live_reconnect_stale_audio_dropped events=%d",
                dropped,
            )
        return original(self)

    original_push_audio = session_cls.push_audio

    def push_audio(self, frame):
        started = getattr(self, "_session_started_fut", None)
        if started is not None and not started.done():
            # Keep only the most recent stretch: enough to hold the start of
            # what the caller says while a character session starts (iPhone
            # 12, 2026-10-11 08:17Z: "Transfer me…" spoken 1 s into a restore
            # was lost), too little to trip the gateway's rate limit.
            held = getattr(self, "_openbase_prestart_frames", None)
            if held is None:
                held = self._openbase_prestart_frames = deque()
                self._openbase_prestart_held_ms = 0.0
                self._openbase_prestart_audio_dropped = 0
                logger.info("dispatch_timing stage=live_prestart_audio_holding")
            held.append(frame)
            self._openbase_prestart_held_ms += _frame_ms(frame)
            while held and self._openbase_prestart_held_ms > PRESTART_AUDIO_KEEP_MS:
                self._openbase_prestart_held_ms -= _frame_ms(held.popleft())
                self._openbase_prestart_audio_dropped += 1
            return None
        held = getattr(self, "_openbase_prestart_frames", None)
        if held is not None:
            self._openbase_prestart_frames = None
            logger.info(
                "dispatch_timing stage=live_prestart_audio_dropped frames=%d kept=%d",
                getattr(self, "_openbase_prestart_audio_dropped", 0),
                len(held),
            )
            while held:
                original_push_audio(self, held.popleft())
        return original_push_audio(self, frame)

    session_cls._reset_for_reconnect = _reset_for_reconnect
    session_cls.push_audio = push_audio
    session_cls._openbase_reconnect_patched = True
    return True
