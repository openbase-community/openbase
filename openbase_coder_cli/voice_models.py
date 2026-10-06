"""Voice model catalog: which engine renders the voice side of a call.

The voice model is picked like the agent model (Claude Fable, Opus, ...):
one selectable id whose engine follows from it. ``gpt-live-1`` runs the
Live Voice engine (OpenAI GPT-Live full duplex with client delegation to
Super Agent threads); ``pipeline`` runs the classic STT -> Super Agent turn
-> TTS pipeline using the configured STT and TTS providers. See
``dev-docs/live-voice.md``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

VoiceModelId = Literal["gpt-live-1", "pipeline"]
VoiceEngineId = Literal["live", "pipeline"]
LiveVoiceProviderId = Literal["openbase_cloud", "openai"]

GPT_LIVE_VOICE_MODEL_ID = "gpt-live-1"
PIPELINE_VOICE_MODEL_ID = "pipeline"
DEFAULT_VOICE_MODEL_ID: VoiceModelId = GPT_LIVE_VOICE_MODEL_ID

VOICE_ENGINE_LIVE = "live"
VOICE_ENGINE_PIPELINE = "pipeline"

OPENBASE_CLOUD_LIVE_VOICE_PROVIDER_ID = "openbase_cloud"
OPENAI_LIVE_VOICE_PROVIDER_ID = "openai"
DEFAULT_LIVE_VOICE_PROVIDER_ID: LiveVoiceProviderId = (
    OPENBASE_CLOUD_LIVE_VOICE_PROVIDER_ID
)

# Environment overrides, mirroring LIVEKIT_STT_PROVIDER / LIVEKIT_TTS_PROVIDER.
VOICE_MODEL_ENV_KEY = "LIVEKIT_VOICE_MODEL"
LIVE_VOICE_PROVIDER_ENV_KEY = "LIVEKIT_LIVE_VOICE_PROVIDER"


@dataclass(frozen=True)
class VoiceModelOption:
    id: VoiceModelId
    label: str
    description: str
    engine: VoiceEngineId
    is_default: bool = False

    def payload(self) -> dict[str, str | bool]:
        return asdict(self)


VOICE_MODEL_OPTIONS: tuple[VoiceModelOption, ...] = (
    VoiceModelOption(
        GPT_LIVE_VOICE_MODEL_ID,
        "GPT-Live 1",
        (
            "OpenAI's full-duplex voice model: it listens and speaks at the "
            "same time while Super Agents do the work. Runs through Openbase "
            "Cloud by default, or your own OpenAI key."
        ),
        VOICE_ENGINE_LIVE,
        is_default=True,
    ),
    VoiceModelOption(
        PIPELINE_VOICE_MODEL_ID,
        "Classic pipeline",
        (
            "Speech-to-text, then the agent's turn, then text-to-speech using "
            "your STT and TTS provider settings. The only option for local-only "
            "audio."
        ),
        VOICE_ENGINE_PIPELINE,
    ),
)

_VOICE_MODELS_BY_ID = {option.id: option for option in VOICE_MODEL_OPTIONS}


@dataclass(frozen=True)
class LiveVoiceProviderOption:
    id: LiveVoiceProviderId
    label: str
    description: str
    is_default: bool = False

    def payload(self) -> dict[str, str | bool]:
        return asdict(self)


LIVE_VOICE_PROVIDER_OPTIONS: tuple[LiveVoiceProviderOption, ...] = (
    LiveVoiceProviderOption(
        OPENBASE_CLOUD_LIVE_VOICE_PROVIDER_ID,
        "Openbase Cloud",
        "GPT-Live through your Openbase account; no OpenAI key needed.",
        is_default=True,
    ),
    LiveVoiceProviderOption(
        OPENAI_LIVE_VOICE_PROVIDER_ID,
        "OpenAI (your key)",
        "GPT-Live straight from OpenAI with the OPENAI_API_KEY in ~/.openbase/.env.",
    ),
)


def normalize_voice_model_id(model_id: str | None) -> VoiceModelId:
    normalized = (model_id or DEFAULT_VOICE_MODEL_ID).strip().lower()
    if normalized in {"live", "gpt-live", "gpt_live", "gpt-live-1", "modern"}:
        normalized = GPT_LIVE_VOICE_MODEL_ID
    if normalized in {"classic", "stt-tts", "stt_tts"}:
        normalized = PIPELINE_VOICE_MODEL_ID
    if normalized not in _VOICE_MODELS_BY_ID:
        raise ValueError(
            "Voice model must be one of: "
            + ", ".join(option.id for option in VOICE_MODEL_OPTIONS)
            + "."
        )
    return normalized  # type: ignore[return-value]


def voice_engine_for_model(model_id: str | None) -> VoiceEngineId:
    return _VOICE_MODELS_BY_ID[normalize_voice_model_id(model_id)].engine


def voice_model_option(model_id: str | None) -> VoiceModelOption:
    return _VOICE_MODELS_BY_ID[normalize_voice_model_id(model_id)]


def voice_model_options_payload() -> list[dict[str, str | bool]]:
    return [option.payload() for option in VOICE_MODEL_OPTIONS]


def normalize_live_voice_provider_id(provider_id: str | None) -> LiveVoiceProviderId:
    normalized = (provider_id or DEFAULT_LIVE_VOICE_PROVIDER_ID).strip().lower()
    if normalized in {"cloud", "openbase", "openbase-cloud"}:
        normalized = OPENBASE_CLOUD_LIVE_VOICE_PROVIDER_ID
    if normalized not in {option.id for option in LIVE_VOICE_PROVIDER_OPTIONS}:
        raise ValueError(
            "Live voice provider must be one of: openbase_cloud, openai."
        )
    return normalized  # type: ignore[return-value]


def live_voice_provider_options_payload() -> list[dict[str, str | bool]]:
    return [option.payload() for option in LIVE_VOICE_PROVIDER_OPTIONS]
