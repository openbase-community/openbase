"""Diagnostic STT and VAD wrappers that log audio ingress and speech events."""

import logging
import time

from livekit import rtc
from livekit.agents import (
    stt as livekit_stt,
)
from livekit.agents import (
    vad as livekit_vad,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN

from openbase_coder_cli.livekit_agent.logging_utils import (
    _event_text_hash,
    _frame_duration_ms,
    _should_log_audio_frame,
)

logger = logging.getLogger(__name__)

# When no inbound audio frame is pushed into the STT stream for at least this
# long, log an explicit gap event. A gap is the desktop-side smoking gun for a
# dropped/chopped user utterance: the phone kept capturing speech locally, but
# its uplink stalled (Wi-Fi roam, ICE/TURN path change, packet loss) so the
# server's transcriber never received those words. LiveKit pushes ~20-50ms
# frames continuously while a track is live, so a sub-second gap is anomalous.
INBOUND_AUDIO_GAP_LOG_THRESHOLD_SECONDS = 0.4


class LoggingSTT(livekit_stt.STT):
    """Diagnostic STT wrapper that logs audio ingress and speech events."""

    def __init__(self, wrapped: livekit_stt.STT) -> None:
        super().__init__(capabilities=wrapped.capabilities)
        self._wrapped = wrapped
        self._stream_count = 0
        self._wrapped.on("metrics_collected", self._forward_metrics)
        self._wrapped.on("error", self._forward_error)
        logger.info(
            "dispatch_timing stage=stt_initialized provider=%s model=%s label=%s "
            "streaming=%s interim_results=%s diarization=%s aligned_transcript=%s "
            "offline_recognize=%s",
            self.provider,
            self.model,
            self.label,
            self.capabilities.streaming,
            self.capabilities.interim_results,
            self.capabilities.diarization,
            self.capabilities.aligned_transcript,
            self.capabilities.offline_recognize,
        )

    @property
    def label(self) -> str:
        return self._wrapped.label

    @property
    def model(self) -> str:
        return self._wrapped.model

    @property
    def provider(self) -> str:
        return self._wrapped.provider

    def _forward_metrics(self, metrics) -> None:
        self.emit("metrics_collected", metrics)

    def _forward_error(self, error) -> None:
        self.emit("error", error)

    async def _recognize_impl(
        self,
        buffer,
        *,
        language=NOT_GIVEN,
        conn_options=DEFAULT_API_CONNECT_OPTIONS,
    ) -> livekit_stt.SpeechEvent:
        logger.info(
            "dispatch_timing stage=stt_recognize_start provider=%s model=%s "
            "language=%s conn_options=%s buffer_type=%s",
            self.provider,
            self.model,
            language if language is not NOT_GIVEN else "",
            type(conn_options).__name__,
            type(buffer).__name__,
        )
        event = await self._wrapped.recognize(
            buffer,
            language=language,
            conn_options=conn_options,
        )
        _log_stt_event(
            "stt_recognize_result", event, provider=self.provider, model=self.model
        )
        return event

    def stream(
        self,
        *,
        language=NOT_GIVEN,
        conn_options=DEFAULT_API_CONNECT_OPTIONS,
    ):
        self._stream_count += 1
        stream_id = f"stt-{self._stream_count}"
        logger.info(
            "dispatch_timing stage=stt_stream_create stream_id=%s provider=%s "
            "model=%s language=%s conn_options=%s",
            stream_id,
            self.provider,
            self.model,
            language if language is not NOT_GIVEN else "",
            type(conn_options).__name__,
        )
        return LoggingRecognizeStream(
            self._wrapped.stream(language=language, conn_options=conn_options),
            stream_id=stream_id,
            provider=self.provider,
            model=self.model,
        )

    def prewarm(self) -> None:
        logger.info(
            "dispatch_timing stage=stt_prewarm provider=%s model=%s",
            self.provider,
            self.model,
        )
        self._wrapped.prewarm()

    async def aclose(self) -> None:
        logger.info(
            "dispatch_timing stage=stt_close provider=%s model=%s streams_created=%d",
            self.provider,
            self.model,
            self._stream_count,
        )
        await self._wrapped.aclose()


class LoggingRecognizeStream:
    def __init__(self, stream, *, stream_id: str, provider: str, model: str) -> None:
        self._stream = stream
        self._stream_id = stream_id
        self._provider = provider
        self._model = model
        self._frame_count = 0
        self._sample_count = 0
        self._flush_count = 0
        self._event_count = 0
        self._last_frame_monotonic: float | None = None

    @property
    def start_time_offset(self) -> float:
        return self._stream.start_time_offset

    @start_time_offset.setter
    def start_time_offset(self, value: float) -> None:
        self._stream.start_time_offset = value

    @property
    def start_time(self) -> float:
        return self._stream.start_time

    @start_time.setter
    def start_time(self, value: float) -> None:
        self._stream.start_time = value

    def push_frame(self, frame: rtc.AudioFrame) -> None:
        now = time.monotonic()
        if self._last_frame_monotonic is not None:
            gap_seconds = now - self._last_frame_monotonic
            if gap_seconds >= INBOUND_AUDIO_GAP_LOG_THRESHOLD_SECONDS:
                sample_rate = getattr(frame, "sample_rate", 0) or 0
                total_audio_ms = (
                    int(self._sample_count / sample_rate * 1000) if sample_rate else 0
                )
                # WARNING level: a starved transcriber input is exactly the
                # condition that silently drops or chops a spoken utterance.
                logger.warning(
                    "dispatch_timing stage=stt_audio_gap stream_id=%s provider=%s "
                    "model=%s gap_ms=%d frames_before_gap=%d total_audio_ms=%d",
                    self._stream_id,
                    self._provider,
                    self._model,
                    int(gap_seconds * 1000),
                    self._frame_count,
                    total_audio_ms,
                )
        self._last_frame_monotonic = now
        self._frame_count += 1
        self._sample_count += getattr(frame, "samples_per_channel", 0) or 0
        if _should_log_audio_frame(self._frame_count):
            sample_rate = getattr(frame, "sample_rate", 0) or 0
            total_audio_ms = (
                int(self._sample_count / sample_rate * 1000) if sample_rate else 0
            )
            logger.info(
                "dispatch_timing stage=stt_audio_frame stream_id=%s provider=%s "
                "model=%s frame_count=%d sample_rate=%s num_channels=%s "
                "samples_per_channel=%s frame_duration_ms=%d total_audio_ms=%d",
                self._stream_id,
                self._provider,
                self._model,
                self._frame_count,
                getattr(frame, "sample_rate", ""),
                getattr(frame, "num_channels", ""),
                getattr(frame, "samples_per_channel", ""),
                _frame_duration_ms(frame),
                total_audio_ms,
            )
        self._stream.push_frame(frame)

    def flush(self) -> None:
        self._flush_count += 1
        logger.info(
            "dispatch_timing stage=stt_stream_flush stream_id=%s provider=%s "
            "model=%s flush_count=%d frame_count=%d sample_count=%d",
            self._stream_id,
            self._provider,
            self._model,
            self._flush_count,
            self._frame_count,
            self._sample_count,
        )
        self._stream.flush()

    def end_input(self) -> None:
        logger.info(
            "dispatch_timing stage=stt_stream_end_input stream_id=%s provider=%s "
            "model=%s frame_count=%d sample_count=%d flush_count=%d",
            self._stream_id,
            self._provider,
            self._model,
            self._frame_count,
            self._sample_count,
            self._flush_count,
        )
        self._stream.end_input()

    async def aclose(self) -> None:
        logger.info(
            "dispatch_timing stage=stt_stream_close stream_id=%s provider=%s "
            "model=%s frame_count=%d sample_count=%d event_count=%d flush_count=%d",
            self._stream_id,
            self._provider,
            self._model,
            self._frame_count,
            self._sample_count,
            self._event_count,
            self._flush_count,
        )
        await self._stream.aclose()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            event = await self._stream.__anext__()
        except StopAsyncIteration:
            logger.info(
                "dispatch_timing stage=stt_stream_iter_end stream_id=%s provider=%s "
                "model=%s frame_count=%d sample_count=%d event_count=%d flush_count=%d",
                self._stream_id,
                self._provider,
                self._model,
                self._frame_count,
                self._sample_count,
                self._event_count,
                self._flush_count,
            )
            raise
        self._event_count += 1
        _log_stt_event(
            "stt_stream_event",
            event,
            provider=self._provider,
            model=self._model,
            stream_id=self._stream_id,
            event_count=self._event_count,
        )
        return event

    async def __aenter__(self):
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, exc_tb) -> None:
        await self._stream.__aexit__(exc_type, exc, exc_tb)


def _log_stt_event(
    stage: str,
    event: livekit_stt.SpeechEvent,
    *,
    provider: str,
    model: str,
    stream_id: str = "",
    event_count: int = 0,
) -> None:
    alternative = event.alternatives[0] if event.alternatives else None
    text = alternative.text if alternative is not None else ""
    usage = event.recognition_usage
    logger.info(
        "dispatch_timing stage=%s stream_id=%s provider=%s model=%s event_count=%d "
        "event_type=%s request_id=%s alternatives=%d text_len=%d text_hash=%s "
        "text_excerpt=%r language=%s confidence=%s speaker_id=%s "
        "speech_start_time=%s alt_start_time=%s alt_end_time=%s "
        "usage_audio_duration=%s usage_input_tokens=%s usage_output_tokens=%s",
        stage,
        stream_id,
        provider,
        model,
        event_count,
        event.type,
        event.request_id,
        len(event.alternatives),
        len(text),
        _event_text_hash(text),
        text[:160],
        getattr(alternative, "language", "") if alternative is not None else "",
        getattr(alternative, "confidence", "") if alternative is not None else "",
        getattr(alternative, "speaker_id", "") if alternative is not None else "",
        event.speech_start_time,
        getattr(alternative, "start_time", "") if alternative is not None else "",
        getattr(alternative, "end_time", "") if alternative is not None else "",
        getattr(usage, "audio_duration", "") if usage is not None else "",
        getattr(usage, "input_tokens", "") if usage is not None else "",
        getattr(usage, "output_tokens", "") if usage is not None else "",
    )


class LoggingVAD(livekit_vad.VAD):
    """Diagnostic VAD wrapper that logs audio ingress and speech boundary events."""

    def __init__(self, wrapped: livekit_vad.VAD) -> None:
        super().__init__(capabilities=wrapped.capabilities)
        self._wrapped = wrapped
        self._stream_count = 0
        self._wrapped.on("metrics_collected", self._forward_metrics)
        logger.info(
            "dispatch_timing stage=vad_initialized provider=%s model=%s "
            "update_interval=%s",
            self.provider,
            self.model,
            self.capabilities.update_interval,
        )

    @property
    def model(self) -> str:
        return self._wrapped.model

    @property
    def provider(self) -> str:
        return self._wrapped.provider

    def _forward_metrics(self, metrics) -> None:
        self.emit("metrics_collected", metrics)

    def stream(self):
        self._stream_count += 1
        stream_id = f"vad-{self._stream_count}"
        logger.info(
            "dispatch_timing stage=vad_stream_create stream_id=%s provider=%s model=%s",
            stream_id,
            self.provider,
            self.model,
        )
        return LoggingVADStream(
            self._wrapped.stream(),
            stream_id=stream_id,
            provider=self.provider,
            model=self.model,
        )


class LoggingVADStream:
    def __init__(self, stream, *, stream_id: str, provider: str, model: str) -> None:
        self._stream = stream
        self._stream_id = stream_id
        self._provider = provider
        self._model = model
        self._frame_count = 0
        self._sample_count = 0
        self._event_count = 0
        self._flush_count = 0

    def push_frame(self, frame: rtc.AudioFrame) -> None:
        self._frame_count += 1
        self._sample_count += getattr(frame, "samples_per_channel", 0) or 0
        if _should_log_audio_frame(self._frame_count):
            sample_rate = getattr(frame, "sample_rate", 0) or 0
            total_audio_ms = (
                int(self._sample_count / sample_rate * 1000) if sample_rate else 0
            )
            logger.info(
                "dispatch_timing stage=vad_audio_frame stream_id=%s provider=%s "
                "model=%s frame_count=%d sample_rate=%s num_channels=%s "
                "samples_per_channel=%s frame_duration_ms=%d total_audio_ms=%d",
                self._stream_id,
                self._provider,
                self._model,
                self._frame_count,
                getattr(frame, "sample_rate", ""),
                getattr(frame, "num_channels", ""),
                getattr(frame, "samples_per_channel", ""),
                _frame_duration_ms(frame),
                total_audio_ms,
            )
        self._stream.push_frame(frame)

    def flush(self) -> None:
        self._flush_count += 1
        logger.info(
            "dispatch_timing stage=vad_stream_flush stream_id=%s provider=%s "
            "model=%s flush_count=%d frame_count=%d sample_count=%d",
            self._stream_id,
            self._provider,
            self._model,
            self._flush_count,
            self._frame_count,
            self._sample_count,
        )
        self._stream.flush()

    def end_input(self) -> None:
        logger.info(
            "dispatch_timing stage=vad_stream_end_input stream_id=%s provider=%s "
            "model=%s frame_count=%d sample_count=%d flush_count=%d",
            self._stream_id,
            self._provider,
            self._model,
            self._frame_count,
            self._sample_count,
            self._flush_count,
        )
        self._stream.end_input()

    async def aclose(self) -> None:
        logger.info(
            "dispatch_timing stage=vad_stream_close stream_id=%s provider=%s "
            "model=%s frame_count=%d sample_count=%d event_count=%d flush_count=%d",
            self._stream_id,
            self._provider,
            self._model,
            self._frame_count,
            self._sample_count,
            self._event_count,
            self._flush_count,
        )
        await self._stream.aclose()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            event = await self._stream.__anext__()
        except StopAsyncIteration:
            logger.info(
                "dispatch_timing stage=vad_stream_iter_end stream_id=%s provider=%s "
                "model=%s frame_count=%d sample_count=%d event_count=%d flush_count=%d",
                self._stream_id,
                self._provider,
                self._model,
                self._frame_count,
                self._sample_count,
                self._event_count,
                self._flush_count,
            )
            raise
        self._event_count += 1
        logger.info(
            "dispatch_timing stage=vad_stream_event stream_id=%s provider=%s "
            "model=%s event_count=%d event_type=%s samples_index=%s timestamp=%s "
            "speaking=%s probability=%s speech_duration=%s silence_duration=%s "
            "inference_duration=%s raw_accumulated_speech=%s raw_accumulated_silence=%s "
            "frames=%d",
            self._stream_id,
            self._provider,
            self._model,
            self._event_count,
            event.type,
            event.samples_index,
            event.timestamp,
            event.speaking,
            event.probability,
            event.speech_duration,
            event.silence_duration,
            event.inference_duration,
            event.raw_accumulated_speech,
            event.raw_accumulated_silence,
            len(event.frames),
        )
        return event
