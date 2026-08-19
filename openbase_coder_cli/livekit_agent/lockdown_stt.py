"""STT boundary that consumes armed safe-phrase utterances before logging."""

from __future__ import annotations

import time
from collections.abc import Callable

from livekit.agents import stt as livekit_stt
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN

from openbase_coder_cli.voice_lockdown.broker import VoiceLockdownBroker


class VoiceLockdownSTT(livekit_stt.STT):
    """Wrap the raw provider; challenge transcripts never reach outer wrappers."""

    def __init__(
        self,
        wrapped: livekit_stt.STT,
        *,
        broker: VoiceLockdownBroker,
        room_sid: str,
        participant_identity: str,
        on_outcome: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(capabilities=wrapped.capabilities)
        self._wrapped = wrapped
        self._broker = broker
        self._room_sid = room_sid
        self._participant_identity = participant_identity
        self._on_outcome = on_outcome or (lambda _outcome: None)
        self._wrapped.on("metrics_collected", lambda metrics: self.emit("metrics_collected", metrics))
        self._wrapped.on("error", lambda error: self.emit("error", error))

    @property
    def label(self) -> str:
        return self._wrapped.label

    @property
    def model(self) -> str:
        return self._wrapped.model

    @property
    def provider(self) -> str:
        return self._wrapped.provider

    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        event = await self._wrapped.recognize(buffer, language=language, conn_options=conn_options)
        if self._armed():
            raise RuntimeError("Offline transcription is disabled while a safe-phrase challenge is armed.")
        return event

    def stream(self, *, language=NOT_GIVEN, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        return VoiceLockdownStream(
            self._wrapped.stream(language=language, conn_options=conn_options),
            broker=self._broker,
            room_sid=self._room_sid,
            participant_identity=self._participant_identity,
            on_outcome=self._on_outcome,
        )

    def _armed(self) -> bool:
        return bool(
            self._room_sid
            and self._participant_identity
            and self._broker.is_armed(
                room_sid=self._room_sid,
                participant_identity=self._participant_identity,
            )
        )

    def prewarm(self) -> None:
        self._wrapped.prewarm()

    async def aclose(self) -> None:
        await self._wrapped.aclose()


class VoiceLockdownStream:
    def __init__(self, stream, *, broker, room_sid, participant_identity, on_outcome) -> None:
        self._stream = stream
        self._broker = broker
        self._room_sid = room_sid
        self._participant_identity = participant_identity
        self._on_outcome = on_outcome
        self._suppress_transcripts_until = 0.0

    @property
    def start_time_offset(self):
        return self._stream.start_time_offset

    @start_time_offset.setter
    def start_time_offset(self, value):
        self._stream.start_time_offset = value

    @property
    def start_time(self):
        return self._stream.start_time

    @start_time.setter
    def start_time(self, value):
        self._stream.start_time = value

    def push_frame(self, frame) -> None:
        self._stream.push_frame(frame)

    def flush(self) -> None:
        self._stream.flush()

    def end_input(self) -> None:
        self._stream.end_input()

    async def aclose(self) -> None:
        await self._stream.aclose()

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            event = await self._stream.__anext__()
            if event.type == livekit_stt.SpeechEventType.START_OF_SPEECH:
                self._suppress_transcripts_until = 0.0
            armed = bool(
                self._room_sid
                and self._participant_identity
                and self._broker.is_armed(
                    room_sid=self._room_sid,
                    participant_identity=self._participant_identity,
                )
            )
            transcript_event = event.type in {
                livekit_stt.SpeechEventType.INTERIM_TRANSCRIPT,
                livekit_stt.SpeechEventType.FINAL_TRANSCRIPT,
            }
            if not armed and not (
                transcript_event and time.monotonic() < self._suppress_transcripts_until
            ):
                return event
            if event.type == livekit_stt.SpeechEventType.FINAL_TRANSCRIPT:
                if armed:
                    transcript = event.alternatives[0].text if event.alternatives else ""
                    outcome = self._broker.verify_final_utterance(
                        transcript,
                        room_sid=self._room_sid,
                        participant_identity=self._participant_identity,
                    )
                    self._on_outcome(outcome)
                # Some providers emit an unformatted/formatted final pair.
                # The outer deduper never sees the consumed first copy, so
                # retain a short local suppression window until new speech.
                self._suppress_transcripts_until = time.monotonic() + 3.0
            # Suppress both interim and final challenge speech. Other audio
            # lifecycle events contain no transcript and can pass through.
            if event.type not in {
                livekit_stt.SpeechEventType.INTERIM_TRANSCRIPT,
                livekit_stt.SpeechEventType.FINAL_TRANSCRIPT,
            }:
                return event

    async def __aenter__(self):
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, exc_tb) -> None:
        await self._stream.__aexit__(exc_type, exc, exc_tb)
