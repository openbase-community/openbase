"""Voice-selecting TTS wrapper and speech-formatting synthesis stream."""

import hashlib
import logging
import time
from collections.abc import Callable

from livekit.agents import (
    tts as livekit_tts,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from openbase_coder_cli.livekit_agent.config import LIVEKIT_VERBOSE_LOGGING
from openbase_coder_cli.livekit_agent.speech_formatter import format_for_speech
from openbase_coder_cli.tts_providers import (
    CARTESIA_PROVIDER_ID,
    DEFAULT_CARTESIA_TTS_VOLUME,
    get_tts_provider,
)

logger = logging.getLogger(__name__)


def text_for_tts(text: str) -> str:
    spoken_text = format_for_speech(text)
    return spoken_text or "Technical output omitted, shown on screen."


class VoiceSelectingTTS(livekit_tts.TTS):
    def __init__(
        self,
        *,
        default_voice_id: str,
        default_voice_name: str | None = None,
        active_voice_id,
        active_voice_name=None,
        api_key: str | None = None,
        api_key_provider: Callable[[], str | None] | None = None,
        provider=None,
        role: str = "direct",
        model: str = "sonic-3",
        volume: float = DEFAULT_CARTESIA_TTS_VOLUME,
        base_url: str | None = None,
        api_version: str | None = None,
        delivery_ledger=None,
    ) -> None:
        self._provider = provider or get_tts_provider(CARTESIA_PROVIDER_ID)
        default_tts = self._provider.create_livekit_tts(
            voice_id=default_voice_id,
            api_key=api_key,
            base_url=base_url,
            api_version=api_version,
            model=model,
            volume=volume,
        )
        super().__init__(
            capabilities=default_tts.capabilities,
            sample_rate=default_tts.sample_rate,
            num_channels=default_tts.num_channels,
        )
        self._default_voice_id = default_voice_id
        self._default_voice_name = default_voice_name
        self._active_voice_id = active_voice_id
        self._active_voice_name = active_voice_name or (lambda: None)
        self._api_key = api_key
        self._api_key_provider = api_key_provider
        self._role = role
        self._model = model
        self._volume = volume
        self._base_url = base_url
        self._api_version = api_version
        self._delivery_ledger = delivery_ledger
        self._tts_by_voice_id: dict[str, livekit_tts.TTS] = {
            default_voice_id: default_tts
        }
        logger.info(
            "dispatch_timing stage=tts_initialized role=%s provider=%s model=%s "
            "default_voice_id=%s default_voice_name=%s api_key_configured=%s "
            "volume=%s sample_rate=%s num_channels=%s",
            self._role,
            self._provider.provider_id,
            self._model,
            self._default_voice_id,
            self._default_voice_name or "",
            bool(self._api_key),
            self._volume,
            self.sample_rate,
            self.num_channels,
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return self._provider.display_name

    def synthesize(
        self,
        text: str,
        *,
        conn_options=DEFAULT_API_CONNECT_OPTIONS,
    ):
        return self.synthesize_with_voice(
            text,
            voice_id=self._active_voice_id(),
            conn_options=conn_options,
        )

    def synthesize_with_voice(
        self,
        text: str,
        *,
        voice_id: str | None,
        conn_options=DEFAULT_API_CONNECT_OPTIONS,
    ):
        spoken_text = text_for_tts(text)
        self._log_tts(
            stage="tts_synthesize_start",
            voice_id=voice_id,
            spoken_text=spoken_text,
        )
        return self._tts_for_voice(voice_id).synthesize(
            spoken_text,
            conn_options=conn_options,
        )

    def stream(
        self,
        *,
        conn_options=DEFAULT_API_CONNECT_OPTIONS,
    ):
        voice_id = self._active_voice_id()
        resolved_voice_id = self.resolve_voice_id(voice_id)
        logger.info(
            "dispatch_timing stage=tts_stream_start role=%s requested_voice_id=%s "
            "resolved_voice_id=%s voice_name=%s conn_options=%s",
            self._role,
            voice_id or "",
            resolved_voice_id,
            self._voice_name_for_id(resolved_voice_id) or "",
            type(conn_options).__name__,
        )
        return SpeechFormattingSynthesizeStream(
            self._tts_for_voice(resolved_voice_id).stream(
                conn_options=conn_options,
            ),
            role=self._role,
            voice_id=resolved_voice_id,
            voice_name=self._voice_name_for_id(resolved_voice_id),
            delivery_ledger=self._delivery_ledger,
        )

    def prewarm(self) -> None:
        voice_id = self._active_voice_id()
        logger.info(
            "dispatch_timing stage=tts_prewarm role=%s requested_voice_id=%s "
            "resolved_voice_id=%s voice_name=%s",
            self._role,
            voice_id or "",
            self.resolve_voice_id(voice_id),
            self.resolve_voice_name(voice_id) or "",
        )
        self._tts_for_voice(self._active_voice_id()).prewarm()

    def resolve_voice_id(self, voice_id: str | None) -> str:
        return voice_id or self._default_voice_id

    def resolve_voice_name(self, voice_id: str | None) -> str | None:
        return self._voice_name_for_id(self.resolve_voice_id(voice_id))

    def _refresh_api_key(self) -> None:
        """Pull a current credential before synthesis begins.

        Openbase Cloud audio authenticates each websocket connection with a
        short-lived machine token; the token captured at session start goes
        stale, and every later reconnect would then be rejected with a 403
        and kill the speech. Refreshing per synthesis keeps new connections
        authenticated.
        """
        if self._api_key_provider is None:
            return
        try:
            refreshed = self._api_key_provider()
        except Exception:
            # Keep speaking with the previous token; it may still be valid.
            logger.warning(
                "dispatch_timing stage=tts_api_key_refresh_failed role=%s",
                self._role,
                exc_info=True,
            )
            return
        if not refreshed or refreshed == self._api_key:
            return
        self._api_key = refreshed
        for tts in self._tts_by_voice_id.values():
            opts = getattr(tts, "_opts", None)
            if opts is not None and hasattr(opts, "api_key"):
                opts.api_key = refreshed
        logger.info(
            "dispatch_timing stage=tts_api_key_refreshed role=%s voice_clients=%d",
            self._role,
            len(self._tts_by_voice_id),
        )

    def _tts_for_voice(self, voice_id: str | None) -> livekit_tts.TTS:
        self._refresh_api_key()
        resolved_voice_id = self.resolve_voice_id(voice_id)
        tts = self._tts_by_voice_id.get(resolved_voice_id)
        if tts is None:
            logger.info(
                "dispatch_timing stage=tts_voice_client_create role=%s "
                "provider=%s voice_id=%s voice_name=%s model=%s volume=%s",
                self._role,
                self._provider.provider_id,
                resolved_voice_id,
                self._voice_name_for_id(resolved_voice_id) or "",
                self._model,
                self._volume,
            )
            tts = self._provider.create_livekit_tts(
                voice_id=resolved_voice_id,
                api_key=self._api_key,
                base_url=self._base_url,
                api_version=self._api_version,
                model=self._model,
                volume=self._volume,
            )
            self._tts_by_voice_id[resolved_voice_id] = tts
        return tts

    def _voice_name_for_id(self, voice_id: str | None) -> str | None:
        resolved_voice_id = voice_id or self._default_voice_id
        if resolved_voice_id == self._default_voice_id:
            return self._default_voice_name
        active_voice_id = self._active_voice_id()
        if active_voice_id == resolved_voice_id:
            active_voice_name = self._active_voice_name()
            return active_voice_name if isinstance(active_voice_name, str) else None
        return None

    def _log_tts(
        self,
        *,
        stage: str,
        voice_id: str | None,
        spoken_text: str,
    ) -> None:
        resolved_voice_id = voice_id or self._default_voice_id
        logger.info(
            "dispatch_timing stage=%s role=%s voice_id=%s voice_name=%s "
            "text_len=%d text_hash=%s text_excerpt=%r",
            stage,
            self._role,
            resolved_voice_id,
            self._voice_name_for_id(resolved_voice_id) or "",
            len(spoken_text),
            hashlib.sha256(spoken_text.encode("utf-8")).hexdigest()[:12],
            spoken_text[:160],
        )

    async def aclose(self) -> None:
        logger.info(
            "dispatch_timing stage=tts_close_start role=%s voice_client_count=%d",
            self._role,
            len(self._tts_by_voice_id),
        )
        for tts in self._tts_by_voice_id.values():
            await tts.aclose()
        logger.info("dispatch_timing stage=tts_close_end role=%s", self._role)


VoiceSelectingCartesiaTTS = VoiceSelectingTTS


class SpeechFormattingSynthesizeStream:
    def __init__(
        self,
        stream,
        *,
        role: str,
        voice_id: str | None = None,
        voice_name: str | None = None,
        delivery_ledger=None,
    ) -> None:
        self._stream = stream
        self._buffer = ""
        self._role = role
        self._voice_id = voice_id
        self._voice_name = voice_name
        self._push_count = 0
        self._flush_count = 0
        self._audio_event_count = 0
        self._non_audio_event_count = 0
        self._flushed_text_monotonic: float | None = None
        self._audio_seconds = 0.0
        self._delivery_ledger = delivery_ledger
        self._delivery_record = None

    def push_text(self, token: str) -> None:
        self._buffer += token
        self._push_count += 1
        if LIVEKIT_VERBOSE_LOGGING:
            logger.info(
                "dispatch_timing stage=tts_stream_push_text role=%s voice_id=%s "
                "voice_name=%s push_count=%d token_len=%d buffer_len=%d token_excerpt=%r",
                self._role,
                self._voice_id or "",
                self._voice_name or "",
                self._push_count,
                len(token),
                len(self._buffer),
                token[:120],
            )

    def flush(self) -> None:
        self._flush_count += 1
        if self._buffer:
            final_text = text_for_tts(self._buffer)
            logger.info(
                "dispatch_timing stage=tts_stream_flush role=%s voice_id=%s "
                "voice_name=%s flush_count=%d original_len=%d text_len=%d "
                "text_hash=%s text_excerpt=%r",
                self._role,
                self._voice_id or "",
                self._voice_name or "",
                self._flush_count,
                len(self._buffer),
                len(final_text),
                hashlib.sha256(final_text.encode("utf-8")).hexdigest()[:12],
                final_text[:160],
            )
            if self._delivery_ledger is not None:
                self._delivery_record = self._delivery_ledger.match_tts_flush(
                    tts_text=final_text,
                    role=self._role,
                    voice_id=self._voice_id,
                    voice_name=self._voice_name,
                )
                if self._delivery_record is None:
                    # Unmatched speech (late steer responses, control
                    # phrases) must still emit lifecycle audio events, or
                    # clients unmute mid-speech.
                    self._delivery_record = self._delivery_ledger.track_unmatched_tts(
                        tts_text=final_text,
                    )
            suppress_stale = (
                self._delivery_record is not None
                and getattr(self._delivery_record, "status", "") == "suppressed_stale"
            )
            if not suppress_stale:
                self._stream.push_text(final_text)
            self._buffer = ""
            if self._flushed_text_monotonic is None:
                self._flushed_text_monotonic = time.monotonic()
        elif LIVEKIT_VERBOSE_LOGGING:
            logger.info(
                "dispatch_timing stage=tts_stream_flush_empty role=%s voice_id=%s "
                "voice_name=%s flush_count=%d",
                self._role,
                self._voice_id or "",
                self._voice_name or "",
                self._flush_count,
            )
        self._stream.flush()
        if LIVEKIT_VERBOSE_LOGGING:
            logger.info(
                "dispatch_timing stage=tts_stream_underlying_flush role=%s voice_id=%s "
                "voice_name=%s flush_count=%d",
                self._role,
                self._voice_id or "",
                self._voice_name or "",
                self._flush_count,
            )

    def end_input(self) -> None:
        if LIVEKIT_VERBOSE_LOGGING:
            logger.info(
                "dispatch_timing stage=tts_stream_end_input role=%s voice_id=%s "
                "voice_name=%s push_count=%d flush_count=%d",
                self._role,
                self._voice_id or "",
                self._voice_name or "",
                self._push_count,
                self._flush_count,
            )
        self.flush()
        self._stream.end_input()

    async def aclose(self) -> None:
        # Terminal outcome for this spoken response: a close with flushed text
        # but zero audio events means the reply was never synthesized (for
        # example it was interrupted or errored before audio was produced).
        logger.info(
            "dispatch_timing stage=tts_stream_close role=%s voice_id=%s "
            "voice_name=%s flush_count=%d audio_events=%d non_audio_events=%d "
            "audio_seconds=%.2f had_flushed_text=%s",
            self._role,
            self._voice_id or "",
            self._voice_name or "",
            self._flush_count,
            self._audio_event_count,
            self._non_audio_event_count,
            self._audio_seconds,
            self._flushed_text_monotonic is not None,
        )
        await self._stream.aclose()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            event = await self._stream.__anext__()
        except StopAsyncIteration:
            logger.info(
                "dispatch_timing stage=tts_stream_iter_end role=%s voice_id=%s "
                "voice_name=%s audio_events=%d non_audio_events=%d "
                "audio_seconds=%.2f",
                self._role,
                self._voice_id or "",
                self._voice_name or "",
                self._audio_event_count,
                self._non_audio_event_count,
                self._audio_seconds,
            )
            if self._delivery_ledger is not None and self._delivery_record is not None:
                self._delivery_ledger.mark_tts_completed(
                    self._delivery_record,
                    audio_events=self._audio_event_count,
                    audio_seconds=self._audio_seconds,
                    role=self._role,
                    voice_id=self._voice_id,
                    voice_name=self._voice_name,
                )
            raise
        frame = getattr(event, "frame", None)
        if frame is not None:
            self._audio_event_count += 1
            sample_rate = getattr(frame, "sample_rate", 0)
            samples_per_channel = getattr(frame, "samples_per_channel", 0)
            if sample_rate:
                self._audio_seconds += samples_per_channel / sample_rate
            if self._audio_event_count == 1:
                latency_ms = (
                    int((time.monotonic() - self._flushed_text_monotonic) * 1000)
                    if self._flushed_text_monotonic is not None
                    else -1
                )
                logger.info(
                    "dispatch_timing stage=tts_stream_first_audio role=%s "
                    "voice_id=%s voice_name=%s flush_to_first_audio_ms=%d",
                    self._role,
                    self._voice_id or "",
                    self._voice_name or "",
                    latency_ms,
                )
                if (
                    self._delivery_ledger is not None
                    and self._delivery_record is not None
                ):
                    self._delivery_ledger.mark_audio_started(
                        self._delivery_record,
                        latency_ms=latency_ms,
                        role=self._role,
                        voice_id=self._voice_id,
                        voice_name=self._voice_name,
                    )
            if LIVEKIT_VERBOSE_LOGGING:
                logger.info(
                    "dispatch_timing stage=tts_audio_frame role=%s voice_id=%s "
                    "voice_name=%s audio_event_count=%d sample_rate=%s "
                    "num_channels=%s samples_per_channel=%s",
                    self._role,
                    self._voice_id or "",
                    self._voice_name or "",
                    self._audio_event_count,
                    getattr(frame, "sample_rate", ""),
                    getattr(frame, "num_channels", ""),
                    getattr(frame, "samples_per_channel", ""),
                )
        else:
            self._non_audio_event_count += 1
            if LIVEKIT_VERBOSE_LOGGING:
                logger.info(
                    "dispatch_timing stage=tts_non_audio_event role=%s voice_id=%s "
                    "voice_name=%s non_audio_event_count=%d event_type=%s",
                    self._role,
                    self._voice_id or "",
                    self._voice_name or "",
                    self._non_audio_event_count,
                    type(event).__name__,
                )
        return event

    async def __aenter__(self):
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, exc_tb) -> None:
        await self._stream.__aexit__(exc_type, exc, exc_tb)
