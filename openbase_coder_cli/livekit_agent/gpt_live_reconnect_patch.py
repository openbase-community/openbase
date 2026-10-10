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

Verified against livekit-plugins-openai 1.8.4: ``GPTLiveSession._msg_ch`` is a
``utils.aio.Chan`` drained by ``_send_task`` after ``session.start``.
"""

from __future__ import annotations

import logging

from livekit.agents.utils.aio.channel import ChanClosed, ChanEmpty

logger = logging.getLogger(__name__)

INPUT_AUDIO_APPEND_TYPE = "session.input_audio.append"


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

    session_cls._reset_for_reconnect = _reset_for_reconnect
    session_cls._openbase_reconnect_patched = True
    return True
