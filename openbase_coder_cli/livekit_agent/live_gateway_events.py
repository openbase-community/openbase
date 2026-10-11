"""One log line per GPT-Live gateway event that explains how a reply ended."""

from __future__ import annotations

import base64
import binascii
import math
from array import array

from openbase_coder_cli.livekit_agent.codex_transport import DISPATCH_TIMING_LOG

# Gateway events worth one log line each: how a response began and ended.
# Audio, transcript and usage deltas stay quiet. A reply that stops mid-word
# with no caller speech and no gate discard (Maritime, 2026-10-10 20:56Z)
# is otherwise unattributable between the model, the proxy and the SDK.
_QUIET_GATEWAY_EVENTS = frozenset(
    {
        "session.output_audio.delta",
        "session.output_transcript.delta",
        "session.input_transcript.delta",
        "session.usage.updated",
        "session.instructions.appended",
        "session.thinking.appended",
        "session.commentary.appended",
    }
)
_LOGGED_RESPONSE_EVENTS = frozenset(
    {
        "response.created",
        "response.completed",
        "response.done",
        "response.cancelled",
        "response.incomplete",
        "response.failed",
        "response.output_audio.done",
        "response.output_item.done",
    }
)


def _compact(value) -> str:
    """A short, space-free rendering of a gateway detail for one log field."""
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, dict):
        return ",".join(f"{k}:{_compact(v)}" for k, v in sorted(value.items()))
    return "".join(str(value).split())[:80]


class OutputTranscriptLog:
    """Log what the model said, per burst of output transcript deltas.

    A reply that reaches the phone as a second of near-silent audio (Maritime,
    2026-10-10 20:56Z and 22:16Z) leaves no trace of what the model actually
    produced; the per-burst transcript shows whether it read the text, cut it
    short, or said something else. Deltas are joined and flushed when
    ``flush_after`` seconds pass without one, or when any other event arrives.
    """

    def __init__(self, log, *, flush_after: float = 1.5) -> None:
        self._log = log
        self._flush_after = flush_after
        self._parts: list[str] = []
        self._timer = None
        self._audio_bytes = 0
        self._audio_peak = 0

    def delta(self, text: str) -> None:
        if text:
            self._parts.append(text)
        self._schedule()

    def audio_delta(self, encoded: str) -> None:
        """Account for one output audio delta (24 kHz mono 16-bit PCM, base64).

        Whether the gateway sent the whole reply's audio, and how loud, is
        what separates a reply the gateway cut from one the adapter's silence
        gate closed early (Maritime, 2026-10-10 23:57Z: a 1.8 s burst for a
        full sentence, first frame at -45 dBFS).
        """
        if not encoded:
            return
        try:
            raw = base64.b64decode(encoded)
        except (ValueError, binascii.Error):
            return
        self._audio_bytes += len(raw)
        samples = array("h")
        samples.frombytes(raw[: len(raw) - len(raw) % 2])
        if samples:
            self._audio_peak = max(self._audio_peak, max(abs(v) for v in samples))
        self._schedule()

    def _schedule(self) -> None:
        import asyncio

        if self._timer is not None:
            self._timer.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._timer = None
            return
        self._timer = loop.call_later(self._flush_after, self.flush)

    def flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._parts and not self._audio_bytes:
            return
        text = "".join(self._parts)
        audio_ms = self._audio_bytes // 48  # 24 kHz mono int16: 48 bytes per ms
        peak_dbfs = 20 * math.log10(max(self._audio_peak / 32768, 1e-6))
        self._parts = []
        self._audio_bytes = 0
        self._audio_peak = 0
        self._log.info(
            "%s stage=live_output_transcript chars=%d audio_ms=%d peak_dbfs=%.1f text=%r",
            DISPATCH_TIMING_LOG,
            len(text),
            audio_ms,
            peak_dbfs,
            text[:160],
        )


def log_gateway_event(
    log, event, transcript: OutputTranscriptLog | None = None
) -> None:
    """One line per response lifecycle, close or error event from the gateway."""
    if not isinstance(event, dict):
        return
    kind = str(event.get("type") or "")
    if transcript is not None:
        if kind == "session.output_transcript.delta":
            transcript.delta(str(event.get("delta") or ""))
            return
        if kind == "session.output_audio.delta":
            transcript.audio_delta(str(event.get("delta") or ""))
            return
        if kind not in _QUIET_GATEWAY_EVENTS:
            transcript.flush()
    if not kind or kind in _QUIET_GATEWAY_EVENTS:
        return
    detail = ""
    if kind == "response.event":
        inner = event.get("event") if isinstance(event.get("event"), dict) else {}
        kind = str(inner.get("type") or "response.?")
        if kind not in _LOGGED_RESPONSE_EVENTS and "error" not in kind:
            return
        response = (
            inner.get("response") if isinstance(inner.get("response"), dict) else {}
        )
        item = inner.get("item") if isinstance(inner.get("item"), dict) else {}
        detail = " status=%s incomplete=%s error=%s item=%s/%s" % (
            response.get("status") or "",
            _compact(response.get("incomplete_details")),
            _compact(response.get("error")),
            item.get("type") or "",
            item.get("status") or "",
        )
    elif kind == "error":
        error = event.get("error") if isinstance(event.get("error"), dict) else {}
        detail = " code=%s error_type=%s" % (
            error.get("code") or "",
            error.get("type") or "",
        )
    elif kind == "session.closed":
        detail = " reason=%s" % _compact(event.get("reason"))
    log.info(
        "%s stage=live_gateway_event type=%s delegation_id=%s%s",
        DISPATCH_TIMING_LOG,
        kind,
        event.get("delegation_id") or "",
        detail,
    )
