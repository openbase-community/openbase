"""LiveKit turn detection wrappers."""

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
LOW_CONFIDENCE_CONTINUATION_GRACE_SECONDS = 6.0


@dataclass(frozen=True)
class UserTurnClosureDecision:
    """Server-owned decision for when the client may mute the user's mic."""

    confidence: float
    source: str
    delay_seconds: float
    completion_reason: str

    @property
    def delay_ms(self) -> int:
        return max(0, int(self.delay_seconds * 1000))


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


_QUESTION_STARTERS = {
    "are",
    "can",
    "could",
    "did",
    "do",
    "does",
    "how",
    "is",
    "should",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "would",
}
_COMMAND_STARTERS = {
    "ask",
    "check",
    "continue",
    "debug",
    "explain",
    "find",
    "fix",
    "go",
    "implement",
    "inspect",
    "investigate",
    "look",
    "open",
    "please",
    "read",
    "restart",
    "run",
    "show",
    "start",
    "stop",
    "tell",
    "test",
    "update",
    "write",
}
_CONTINUATION_TAIL_WORDS = {
    "a",
    "about",
    "also",
    "and",
    "because",
    "but",
    "by",
    "for",
    "from",
    "if",
    "in",
    "like",
    "of",
    "or",
    "so",
    "that",
    "the",
    "then",
    "to",
    "with",
}
_CONTINUATION_TAIL_PHRASES = (
    "and then",
    "but then",
    "for example",
    "i mean",
    "i think",
    "i was",
    "let me",
    "the thing is",
    "what i mean",
)


def decide_user_turn_closure(
    transcript: str,
    *,
    eou_probability: float | None = None,
    signals: UserTurnClosureSignals | None = None,
) -> UserTurnClosureDecision:
    """Decide when the iOS client may auto-mute after an accepted utterance.

    LiveKit has already accepted the utterance before this runs, so the
    decision is intentionally conservative: obvious complete commands/questions
    close quickly, incomplete tails stay open longer, and the turn detector
    probability can override the transcript heuristic when it is available.
    """
    signals = signals or UserTurnClosureSignals(eou_probability=eou_probability)
    eou_probability = signals.eou_probability
    normalized = _normalized_words(transcript)
    if not normalized:
        return UserTurnClosureDecision(
            confidence=0.0,
            source="empty_transcript",
            delay_seconds=3.5,
            completion_reason="empty_transcript_timeout",
        )

    tail = _tail_word(normalized)
    starts_with = normalized.split(" ", 1)[0]
    if _has_incomplete_tail(normalized, tail):
        if eou_probability is not None and eou_probability >= 0.9:
            return UserTurnClosureDecision(
                confidence=eou_probability,
                source="turn_detector_semantic_tail",
                delay_seconds=_delay_with_vad_floor(1.2, signals),
                completion_reason="turn_detector_high_confidence_with_continuation_tail",
            )
        return UserTurnClosureDecision(
            confidence=0.35,
            source=_source_with_vad("semantic_tail", signals),
            delay_seconds=_delay_with_vad_floor(3.5, signals),
            completion_reason="continuation_tail",
        )

    if eou_probability is not None:
        if eou_probability >= 0.75:
            return UserTurnClosureDecision(
                confidence=eou_probability,
                source=_source_with_vad("turn_detector", signals),
                delay_seconds=_delay_with_vad_floor(0.25, signals),
                completion_reason="turn_detector_high_confidence",
            )
        if eou_probability <= 0.45:
            return UserTurnClosureDecision(
                confidence=eou_probability,
                source=_source_with_vad("turn_detector", signals),
                delay_seconds=_delay_with_vad_floor(
                    LOW_CONFIDENCE_CONTINUATION_GRACE_SECONDS,
                    signals,
                ),
                completion_reason="turn_detector_low_confidence",
            )

    if transcript.rstrip().endswith("?") or starts_with in _QUESTION_STARTERS:
        return UserTurnClosureDecision(
            confidence=0.8,
            source=_source_with_vad("semantic_question", signals),
            delay_seconds=_delay_with_vad_floor(0.45, signals),
            completion_reason="complete_question",
        )

    if starts_with in _COMMAND_STARTERS:
        return UserTurnClosureDecision(
            confidence=0.72,
            source=_source_with_vad("semantic_command", signals),
            delay_seconds=_delay_with_vad_floor(0.7, signals),
            completion_reason="complete_command",
        )

    return UserTurnClosureDecision(
        confidence=0.6,
        source=_source_with_vad("semantic_default", signals),
        delay_seconds=_delay_with_vad_floor(1.8, signals),
        completion_reason="default_continuation_grace",
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


def _tail_word(normalized: str) -> str:
    words = normalized.split()
    return words[-1] if words else ""


def _has_incomplete_tail(normalized: str, tail: str) -> bool:
    if tail in _CONTINUATION_TAIL_WORDS:
        return True
    return any(normalized.endswith(phrase) for phrase in _CONTINUATION_TAIL_PHRASES)


def _source_with_vad(source: str, signals: UserTurnClosureSignals) -> str:
    if signals.end_of_turn_delay is not None or signals.transcript_confidence is not None:
        return f"{source}+livekit_metrics"
    return source


def _delay_with_vad_floor(
    delay_seconds: float,
    signals: UserTurnClosureSignals,
) -> float:
    if signals.end_of_turn_delay is None:
        return delay_seconds
    remaining_silence = max(0.0, 0.7 - signals.end_of_turn_delay)
    return max(delay_seconds, remaining_silence)


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
