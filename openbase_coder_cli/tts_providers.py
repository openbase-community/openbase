from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import numpy as np
    from livekit.agents import tts as livekit_tts

from openbase_coder_cli.cartesia_voice_catalog import (
    CARTESIA_VOICE_CATALOG,
    DEFAULT_SUPER_AGENT_VOICE_IDS,
    cartesia_voice_for_id,
)

TTSProviderId = Literal["cartesia", "openbase_cloud", "kokoro"]

CARTESIA_PROVIDER_ID = "cartesia"
OPENBASE_CLOUD_TTS_PROVIDER_ID = "openbase_cloud"
KOKORO_PROVIDER_ID = "kokoro"
DEFAULT_TTS_PROVIDER_ID: TTSProviderId = CARTESIA_PROVIDER_ID
DEFAULT_CARTESIA_VOICE_ID = "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"
DEFAULT_CARTESIA_ANNOUNCER_VOICE_ID = "f786b574-daa5-4673-aa0c-cbe3e8534c02"
DEFAULT_CARTESIA_TTS_VOLUME = 0.8
DEFAULT_KOKORO_VOICE_ID = "af_heart"
DEFAULT_KOKORO_ANNOUNCER_VOICE_ID = "af_bella"
KOKORO_REPO_ID = "hexgrad/Kokoro-82M"
KOKORO_MODEL_FILES = ("config.json", "kokoro-v1_0.pth")
# Cartesia closes idle websockets server-side well before the plugin pool's
# 300s lifetime (observed dead after ~40-90s idle), so a reused pooled socket
# frequently fails its first write and costs an error plus a retry on the
# first reply after a pause. Treat pooled sockets idle longer than this as
# expired so the pool redials instead.
CARTESIA_WS_POOL_MAX_IDLE_SECONDS = 30.0

logger = logging.getLogger(__name__)


def _limit_cartesia_ws_pool_idle(tts_instance: livekit_tts.TTS) -> None:
    pool = getattr(tts_instance, "_pool", None)
    if pool is None or not hasattr(pool, "_max_session_duration"):
        logger.warning(
            "Unable to limit Cartesia websocket pool idle lifetime; "
            "plugin internals changed"
        )
        return
    pool._max_session_duration = CARTESIA_WS_POOL_MAX_IDLE_SECONDS


@dataclass(frozen=True)
class TTSVoice:
    id: str
    name: str
    provider: TTSProviderId
    language: str
    country: str | None = None
    gender: str | None = None

    def payload(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class TTSDownloadStatus:
    provider: TTSProviderId
    ready: bool
    required_files: int
    cached_files: int
    detail: str | None = None

    def payload(self) -> dict[str, str | int | bool | None]:
        return asdict(self)


class BaseTTSProvider:
    provider_id: TTSProviderId
    display_name: str

    def voices(self) -> tuple[TTSVoice, ...]:
        raise NotImplementedError

    def voice_for_id(self, voice_id: str | None) -> TTSVoice | None:
        if not voice_id:
            return None
        return next((voice for voice in self.voices() if voice.id == voice_id), None)

    def voice_for_name(self, name: str | None) -> TTSVoice | None:
        normalized = _normalize_voice_name(name)
        if not normalized:
            return None
        return next(
            (
                voice
                for voice in self.voices()
                if _normalize_voice_name(voice.name) == normalized
            ),
            None,
        )

    def super_agent_voice_for_id(self, voice_id: str | None) -> TTSVoice | None:
        if not voice_id:
            return None
        return next(
            (voice for voice in self.super_agent_voices() if voice.id == voice_id),
            None,
        )

    def super_agent_voice_for_name(self, name: str | None) -> TTSVoice | None:
        normalized = _normalize_voice_name(name)
        if not normalized:
            return None
        return next(
            (
                voice
                for voice in self.super_agent_voices()
                if _normalize_voice_name(voice.name) == normalized
            ),
            None,
        )

    def default_dispatcher_voice(self) -> TTSVoice:
        return self.voices()[0]

    def default_announcer_voice(self) -> TTSVoice:
        return self.default_dispatcher_voice()

    def super_agent_voices(self) -> tuple[TTSVoice, ...]:
        dispatcher_voice_id = self.default_dispatcher_voice().id
        voices = tuple(
            voice for voice in self.voices() if voice.id != dispatcher_voice_id
        )
        return voices or self.voices()

    def catalog_payload(self) -> list[dict[str, str | None]]:
        return [voice.payload() for voice in self.voices()]

    def readiness(self) -> TTSDownloadStatus:
        return TTSDownloadStatus(
            provider=self.provider_id,
            ready=True,
            required_files=0,
            cached_files=0,
        )

    def download_all_voices(self) -> TTSDownloadStatus:
        return self.readiness()

    def create_livekit_tts(self, *, voice_id: str, **kwargs) -> livekit_tts.TTS:
        raise NotImplementedError


class CartesiaTTSProvider(BaseTTSProvider):
    provider_id: TTSProviderId = CARTESIA_PROVIDER_ID
    display_name = "Cartesia"

    def voices(self) -> tuple[TTSVoice, ...]:
        return tuple(
            TTSVoice(
                id=voice.id,
                name=voice.name,
                provider=self.provider_id,
                language=voice.language,
                country=voice.country,
                gender=voice.gender,
            )
            for voice in CARTESIA_VOICE_CATALOG
        )

    def default_dispatcher_voice(self) -> TTSVoice:
        return (
            self.voice_for_id(DEFAULT_CARTESIA_VOICE_ID)
            or super().default_dispatcher_voice()
        )

    def default_announcer_voice(self) -> TTSVoice:
        return (
            self.voice_for_id(DEFAULT_CARTESIA_ANNOUNCER_VOICE_ID)
            or self.default_dispatcher_voice()
        )

    def super_agent_voices(self) -> tuple[TTSVoice, ...]:
        voices = tuple(
            voice
            for voice_id in DEFAULT_SUPER_AGENT_VOICE_IDS
            if (voice := self.voice_for_id(voice_id)) is not None
        )
        return voices or super().super_agent_voices()

    def create_livekit_tts(
        self,
        *,
        voice_id: str,
        api_key: str | None = None,
        base_url: str | None = None,
        api_version: str | None = None,
        model: str = "sonic-3",
        volume: float = DEFAULT_CARTESIA_TTS_VOLUME,
        **kwargs,
    ) -> livekit_tts.TTS:
        from livekit.plugins import cartesia

        cartesia_kwargs: dict[str, str] = {}
        if base_url:
            cartesia_kwargs["base_url"] = base_url
        if api_version:
            cartesia_kwargs["api_version"] = api_version
        tts_instance = cartesia.TTS(
            model=model,
            voice=voice_id,
            api_key=api_key,
            volume=volume,
            **cartesia_kwargs,
        )
        _limit_cartesia_ws_pool_idle(tts_instance)
        return tts_instance


class OpenbaseCloudTTSProvider(CartesiaTTSProvider):
    provider_id: TTSProviderId = OPENBASE_CLOUD_TTS_PROVIDER_ID
    display_name = "Openbase Cloud"


class KokoroTTSProvider(BaseTTSProvider):
    provider_id: TTSProviderId = KOKORO_PROVIDER_ID
    display_name = "Local Kokoro"

    def __init__(self) -> None:
        self._pipelines: dict[str, Any] = {}
        self._pipeline_lock = threading.Lock()

    def voices(self) -> tuple[TTSVoice, ...]:
        return KOKORO_SUPPORTED_VOICE_CATALOG

    def default_dispatcher_voice(self) -> TTSVoice:
        return (
            self.voice_for_id(DEFAULT_KOKORO_VOICE_ID)
            or super().default_dispatcher_voice()
        )

    def default_announcer_voice(self) -> TTSVoice:
        return (
            self.voice_for_id(DEFAULT_KOKORO_ANNOUNCER_VOICE_ID)
            or self.default_dispatcher_voice()
        )

    def super_agent_voices(self) -> tuple[TTSVoice, ...]:
        return self.voices()

    def readiness(self) -> TTSDownloadStatus:
        files = _kokoro_required_files()
        cached = 0
        try:
            from huggingface_hub import hf_hub_download
        except ImportError:
            return TTSDownloadStatus(
                provider=self.provider_id,
                ready=False,
                required_files=len(files),
                cached_files=0,
                detail="Kokoro dependencies are not installed.",
            )

        for filename in files:
            try:
                hf_hub_download(
                    repo_id=KOKORO_REPO_ID,
                    filename=filename,
                    local_files_only=True,
                )
            except Exception:
                continue
            cached += 1

        return TTSDownloadStatus(
            provider=self.provider_id,
            ready=cached == len(files),
            required_files=len(files),
            cached_files=cached,
            detail=None
            if cached == len(files)
            else "Kokoro model or voice files are missing.",
        )

    def download_all_voices(self) -> TTSDownloadStatus:
        files = _kokoro_required_files()
        try:
            from huggingface_hub import hf_hub_download
        except ImportError:
            return TTSDownloadStatus(
                provider=self.provider_id,
                ready=False,
                required_files=len(files),
                cached_files=0,
                detail="Kokoro dependencies are not installed.",
            )

        cached = 0
        for filename in files:
            hf_hub_download(repo_id=KOKORO_REPO_ID, filename=filename)
            cached += 1

        return TTSDownloadStatus(
            provider=self.provider_id,
            ready=True,
            required_files=len(files),
            cached_files=cached,
        )

    def create_livekit_tts(self, *, voice_id: str, **kwargs) -> livekit_tts.TTS:
        from livekit.agents import tts as livekit_tts

        from openbase_coder_cli.tts_kokoro_livekit import KokoroLiveKitTTS

        return livekit_tts.StreamAdapter(
            tts=KokoroLiveKitTTS(
                provider=self,
                voice_id=voice_id,
            ),
        )

    def synthesize_pcm(self, *, text: str, voice_id: str) -> bytes:
        import numpy as np

        voice = self.voice_for_id(voice_id)
        if voice is None:
            raise ValueError(f"Unknown Kokoro voice: {voice_id}")
        pipeline = self._pipeline_for_voice(voice_id)
        chunks: list[np.ndarray] = []
        for result in pipeline(text, voice=voice_id):
            audio = getattr(result, "audio", None)
            if audio is None and isinstance(result, tuple) and len(result) >= 3:
                audio = result[2]
            chunks.append(_audio_array(audio))
        if not chunks:
            return b""
        audio = np.concatenate(chunks)
        audio = np.clip(audio, -1.0, 1.0)
        return (audio * 32767).astype("<i2").tobytes()

    def _pipeline_for_voice(self, voice_id: str):
        lang_code = voice_id.split("_", 1)[0][:1]
        if not lang_code:
            raise ValueError(f"Unable to infer Kokoro language from voice: {voice_id}")
        with self._pipeline_lock:
            pipeline = self._pipelines.get(lang_code)
            if pipeline is None:
                from kokoro import KPipeline

                pipeline = KPipeline(lang_code=lang_code, repo_id=KOKORO_REPO_ID)
                self._pipelines[lang_code] = pipeline
            return pipeline


# KokoroLiveKitTTS/KokoroChunkedStream live in tts_kokoro_livekit so importing
# provider metadata does not pull in livekit.agents; re-export lazily for
# existing importers.
def __getattr__(name: str):
    if name in {"KokoroLiveKitTTS", "KokoroChunkedStream"}:
        from openbase_coder_cli import tts_kokoro_livekit

        return getattr(tts_kokoro_livekit, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_CARTESIA_PROVIDER = CartesiaTTSProvider()
_OPENBASE_CLOUD_PROVIDER = OpenbaseCloudTTSProvider()
_KOKORO_PROVIDER = KokoroTTSProvider()
_PROVIDERS: dict[TTSProviderId, BaseTTSProvider] = {
    CARTESIA_PROVIDER_ID: _CARTESIA_PROVIDER,
    OPENBASE_CLOUD_TTS_PROVIDER_ID: _OPENBASE_CLOUD_PROVIDER,
    KOKORO_PROVIDER_ID: _KOKORO_PROVIDER,
}


def get_tts_provider(provider_id: str | None) -> BaseTTSProvider:
    normalized = normalize_tts_provider_id(provider_id)
    return _PROVIDERS[normalized]


def all_tts_providers() -> tuple[BaseTTSProvider, ...]:
    return tuple(_PROVIDERS.values())


def normalize_tts_provider_id(provider_id: str | None) -> TTSProviderId:
    normalized = (provider_id or DEFAULT_TTS_PROVIDER_ID).strip().lower()
    if normalized in {"openbase", "openbase-cloud", "cloud"}:
        normalized = OPENBASE_CLOUD_TTS_PROVIDER_ID
    if normalized not in _PROVIDERS:
        raise ValueError(
            "TTS provider must be one of: cartesia, openbase_cloud, kokoro."
        )
    return normalized  # type: ignore[return-value]


def voice_name_for_id(provider_id: str | None, voice_id: str | None) -> str | None:
    try:
        provider = get_tts_provider(provider_id)
    except ValueError:
        provider = _CARTESIA_PROVIDER
    voice = provider.voice_for_id(voice_id)
    if voice is not None:
        return voice.name
    if provider.provider_id != CARTESIA_PROVIDER_ID:
        cartesia_voice = cartesia_voice_for_id(voice_id or "")
        return cartesia_voice.name if cartesia_voice else None
    return None


def _kokoro_required_files() -> tuple[str, ...]:
    return tuple(KOKORO_MODEL_FILES) + tuple(
        f"voices/{voice.id}.pt" for voice in KOKORO_SUPPORTED_VOICE_CATALOG
    )


def _audio_array(audio) -> np.ndarray:
    import numpy as np

    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().numpy()
    return np.asarray(audio, dtype=np.float32).reshape(-1)


def _normalize_voice_name(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.casefold().split())


def _kokoro_voice(
    voice_id: str,
    name: str,
    language: str,
    country: str | None,
    gender: str | None,
) -> TTSVoice:
    return TTSVoice(
        id=voice_id,
        name=name,
        provider=KOKORO_PROVIDER_ID,
        language=language,
        country=country,
        gender=gender,
    )


KOKORO_VOICE_CATALOG: tuple[TTSVoice, ...] = (
    _kokoro_voice("af_alloy", "Alloy", "en", "US", "feminine"),
    _kokoro_voice("af_aoede", "Aoede", "en", "US", "feminine"),
    _kokoro_voice("af_bella", "Bella", "en", "US", "feminine"),
    _kokoro_voice("af_heart", "Heart", "en", "US", "feminine"),
    _kokoro_voice("af_jessica", "Jessica", "en", "US", "feminine"),
    _kokoro_voice("af_kore", "Kore", "en", "US", "feminine"),
    _kokoro_voice("af_nicole", "Nicole", "en", "US", "feminine"),
    _kokoro_voice("af_nova", "Nova", "en", "US", "feminine"),
    _kokoro_voice("af_river", "River", "en", "US", "feminine"),
    _kokoro_voice("af_sarah", "Sarah", "en", "US", "feminine"),
    _kokoro_voice("af_sky", "Sky", "en", "US", "feminine"),
    _kokoro_voice("am_adam", "Adam", "en", "US", "masculine"),
    _kokoro_voice("am_echo", "Echo", "en", "US", "masculine"),
    _kokoro_voice("am_eric", "Eric", "en", "US", "masculine"),
    _kokoro_voice("am_fenrir", "Fenrir", "en", "US", "masculine"),
    _kokoro_voice("am_liam", "Liam", "en", "US", "masculine"),
    _kokoro_voice("am_michael", "Michael", "en", "US", "masculine"),
    _kokoro_voice("am_onyx", "Onyx", "en", "US", "masculine"),
    _kokoro_voice("am_puck", "Puck", "en", "US", "masculine"),
    _kokoro_voice("am_santa", "Santa", "en", "US", "masculine"),
    _kokoro_voice("bf_alice", "Alice", "en", "GB", "feminine"),
    _kokoro_voice("bf_emma", "Emma", "en", "GB", "feminine"),
    _kokoro_voice("bf_isabella", "Isabella", "en", "GB", "feminine"),
    _kokoro_voice("bf_lily", "Lily", "en", "GB", "feminine"),
    _kokoro_voice("bm_daniel", "Daniel", "en", "GB", "masculine"),
    _kokoro_voice("bm_fable", "Fable", "en", "GB", "masculine"),
    _kokoro_voice("bm_george", "George", "en", "GB", "masculine"),
    _kokoro_voice("bm_lewis", "Lewis", "en", "GB", "masculine"),
    _kokoro_voice("ef_dora", "Dora", "es", None, "feminine"),
    _kokoro_voice("em_alex", "Alex", "es", None, "masculine"),
    _kokoro_voice("em_santa", "Santa", "es", None, "masculine"),
    _kokoro_voice("ff_siwis", "Siwis", "fr", None, "feminine"),
    _kokoro_voice("hf_alpha", "Alpha", "hi", None, "feminine"),
    _kokoro_voice("hf_beta", "Beta", "hi", None, "feminine"),
    _kokoro_voice("hm_omega", "Omega", "hi", None, "masculine"),
    _kokoro_voice("hm_psi", "Psi", "hi", None, "masculine"),
    _kokoro_voice("if_sara", "Sara", "it", None, "feminine"),
    _kokoro_voice("im_nicola", "Nicola", "it", None, "masculine"),
    _kokoro_voice("jf_alpha", "Alpha", "ja", None, "feminine"),
    _kokoro_voice("jf_gongitsune", "Gongitsune", "ja", None, "feminine"),
    _kokoro_voice("jf_nezumi", "Nezumi", "ja", None, "feminine"),
    _kokoro_voice("jf_tebukuro", "Tebukuro", "ja", None, "feminine"),
    _kokoro_voice("jm_kumo", "Kumo", "ja", None, "masculine"),
    _kokoro_voice("pf_dora", "Dora", "pt", None, "feminine"),
    _kokoro_voice("pm_alex", "Alex", "pt", None, "masculine"),
    _kokoro_voice("pm_santa", "Santa", "pt", None, "masculine"),
    _kokoro_voice("zf_xiaobei", "Xiaobei", "zh", None, "feminine"),
    _kokoro_voice("zf_xiaoni", "Xiaoni", "zh", None, "feminine"),
    _kokoro_voice("zf_xiaoxiao", "Xiaoxiao", "zh", None, "feminine"),
    _kokoro_voice("zf_xiaoyi", "Xiaoyi", "zh", None, "feminine"),
    _kokoro_voice("zm_yunjian", "Yunjian", "zh", None, "masculine"),
    _kokoro_voice("zm_yunxi", "Yunxi", "zh", None, "masculine"),
    _kokoro_voice("zm_yunxia", "Yunxia", "zh", None, "masculine"),
    _kokoro_voice("zm_yunyang", "Yunyang", "zh", None, "masculine"),
)

KOKORO_SUPPORTED_VOICE_CATALOG: tuple[TTSVoice, ...] = tuple(
    voice for voice in KOKORO_VOICE_CATALOG if voice.language == "en"
)
