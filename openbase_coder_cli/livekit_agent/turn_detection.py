"""LiveKit turn detection wrappers.

LiveKit endpointing (VAD + the multilingual turn detector) owns end-of-turn
detection. This module only wraps it: `SafeMultilingualModel` guards against
inference gaps, `VoiceTurnSignalTracker` correlates detector probabilities
with accepted voice turns, and `decide_user_turn_closure` maps that
probability to the mic quiet floor. The floor is a mic-UX policy, not a
second end-of-turn detector: `safe_to_mute_user` is only emitted after the
LiveKit session has reported the user quiet for the full floor.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

from livekit.agents import llm
from livekit.agents.llm.chat_context import ChatMessage
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger(__name__)

_TURN_SIGNAL_TTL_SECONDS = 30.0
_MAX_TURN_SIGNALS = 64

MIN_USER_TURN_QUIET_GRACE_SECONDS = 2.0
LOW_CONFIDENCE_USER_TURN_QUIET_GRACE_SECONDS = 5.0
LOW_EOU_PROBABILITY_THRESHOLD = 0.45


@dataclass(frozen=True)
class UserTurnClosureDecision:
    """Server-owned mic policy: how much verified quiet before auto-mute."""

    confidence: float | None
    source: str
    quiet_grace_seconds: float
    completion_reason: str

    @property
    def quiet_grace_ms(self) -> int:
        return max(0, int(self.quiet_grace_seconds * 1000))


@dataclass(frozen=True)
class TurnDetectorSignal:
    transcript_hash: str
    probability: float
    created_at: float


@dataclass(frozen=True)
class UserTurnClosureSignals:
    eou_probability: float | None = None
    transcript_confidence: float | None = None
    started_speaking_at: float | None = None
    stopped_speaking_at: float | None = None
    transcription_delay: float | None = None
    end_of_turn_delay: float | None = None

    @property
    def silence_ms(self) -> int | None:
        if self.end_of_turn_delay is None:
            return None
        return max(0, int(self.end_of_turn_delay * 1000))


class VoiceTurnSignalTracker:
    """Correlates LiveKit turn-detector output with accepted voice turns."""

    def __init__(
        self,
        *,
        ttl_seconds: float = _TURN_SIGNAL_TTL_SECONDS,
        max_signals: int = _MAX_TURN_SIGNALS,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_signals = max_signals
        self._signals_by_hash: OrderedDict[str, TurnDetectorSignal] = OrderedDict()

    def record_prediction(self, *, transcript: str, probability: float) -> None:
        transcript_hash = normalized_transcript_hash(transcript)
        if not transcript_hash:
            return
        self._prune()
        self._signals_by_hash[transcript_hash] = TurnDetectorSignal(
            transcript_hash=transcript_hash,
            probability=probability,
            created_at=time.monotonic(),
        )
        self._signals_by_hash.move_to_end(transcript_hash)
        while len(self._signals_by_hash) > self._max_signals:
            self._signals_by_hash.popitem(last=False)

    def consume_prediction(self, transcript: str) -> float | None:
        self._prune()
        signal = self._signals_by_hash.pop(normalized_transcript_hash(transcript), None)
        return signal.probability if signal is not None else None

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._ttl_seconds
        stale_hashes = [
            transcript_hash
            for transcript_hash, signal in self._signals_by_hash.items()
            if signal.created_at < cutoff
        ]
        for transcript_hash in stale_hashes:
            self._signals_by_hash.pop(transcript_hash, None)


def decide_user_turn_closure(
    *,
    signals: UserTurnClosureSignals | None = None,
) -> UserTurnClosureDecision:
    """Pick the mic quiet floor for an accepted utterance.

    LiveKit has already committed the turn before this runs. The only input
    that matters is the turn detector's end-of-turn probability: a
    low-confidence end (the user sounded unfinished) gets the long floor,
    everything else gets the minimum floor.
    """
    signals = signals or UserTurnClosureSignals()
    eou_probability = signals.eou_probability
    if eou_probability is not None and eou_probability <= LOW_EOU_PROBABILITY_THRESHOLD:
        return UserTurnClosureDecision(
            confidence=eou_probability,
            source="turn_detector",
            quiet_grace_seconds=LOW_CONFIDENCE_USER_TURN_QUIET_GRACE_SECONDS,
            completion_reason="low_confidence_quiet_floor",
        )
    return UserTurnClosureDecision(
        confidence=eou_probability,
        source="turn_detector"
        if eou_probability is not None
        else "quiet_floor_default",
        quiet_grace_seconds=MIN_USER_TURN_QUIET_GRACE_SECONDS,
        completion_reason="quiet_floor",
    )


def _normalized_words(transcript: str) -> str:
    text = transcript.strip().lower()
    text = re.sub(r"[^\w\s']", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalized_transcript_hash(transcript: str) -> str:
    normalized = _normalized_words(transcript)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def latest_user_turn_signals_from_chat_ctx(
    chat_ctx: llm.ChatContext,
    *,
    turn_signal_tracker: VoiceTurnSignalTracker | None = None,
) -> UserTurnClosureSignals:
    message = latest_user_message(chat_ctx)
    if message is None:
        return UserTurnClosureSignals()
    text = message.text_content or ""
    metrics = getattr(message, "metrics", {}) or {}
    eou_probability = (
        turn_signal_tracker.consume_prediction(text)
        if turn_signal_tracker is not None
        else None
    )
    return UserTurnClosureSignals(
        eou_probability=eou_probability,
        transcript_confidence=getattr(message, "transcript_confidence", None),
        started_speaking_at=_optional_float(metrics.get("started_speaking_at")),
        stopped_speaking_at=_optional_float(metrics.get("stopped_speaking_at")),
        transcription_delay=_optional_float(metrics.get("transcription_delay")),
        end_of_turn_delay=_optional_float(metrics.get("end_of_turn_delay")),
    )


def latest_user_message(chat_ctx: llm.ChatContext) -> ChatMessage | None:
    for item in reversed(chat_ctx.items):
        if isinstance(item, ChatMessage) and item.role == "user":
            return item
    return None


def _optional_float(value: object) -> float | None:
    return value if isinstance(value, int | float) else None


class SafeMultilingualModel(MultilingualModel):
    """Multilingual turn detector with a logged fallback for inference gaps."""

    def __init__(
        self,
        *,
        turn_signal_tracker: VoiceTurnSignalTracker | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._turn_signal_tracker = turn_signal_tracker

    async def predict_end_of_turn(
        self,
        chat_ctx: llm.ChatContext,
        *,
        timeout: float | None = 3,
    ) -> float:
        try:
            probability = await super().predict_end_of_turn(chat_ctx, timeout=timeout)
            if self._turn_signal_tracker is not None:
                message = latest_user_message(chat_ctx)
                if message is not None:
                    self._turn_signal_tracker.record_prediction(
                        transcript=message.text_content or "",
                        probability=probability,
                    )
            return probability
        except AssertionError as exc:
            logger.warning(
                "dispatch_timing stage=turn_detection_fallback "
                "reason=eou_prediction_assertion error=%s",
                exc,
                exc_info=True,
            )
            return 1.0
        except Exception as exc:
            logger.warning(
                "dispatch_timing stage=turn_detection_fallback "
                "reason=eou_prediction_error error=%s",
                exc,
                exc_info=True,
            )
            return 1.0
