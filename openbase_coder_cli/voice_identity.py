"""The one voice identity a user hears, mapped to each voice engine.

The chosen dispatcher voice (a TTS-provider voice, Cartesia by default) is the
identity. The classic pipeline and background announcements speak with it
directly; a GPT-Live call speaks with the same-gender GPT-Live voice paired to
it in ``cartesia_voice_catalog.py``. The pairing runs the other way when an
operator pins the live engine's voice with ``LIVEKIT_LIVE_VOICE_VOICE``: the
call keeps that voice and announcements speak with its best Cartesia match,
so the call still sounds like one person. The default pair, Jacqueline and
marin, leaves existing installs unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from openbase_coder_cli.cartesia_voice_catalog import (
    DEFAULT_GPT_LIVE_VOICE,
    MASCULINE,
    GptLiveVoiceCatalogEntry,
    cartesia_voice_for_gpt_live_voice,
    gpt_live_voice_for_id,
)
from openbase_coder_cli.dispatcher_config import (
    dispatcher_voice,
    selected_voice_engine,
)
from openbase_coder_cli.tts_providers import (
    CARTESIA_PROVIDER_ID,
    OPENBASE_CLOUD_TTS_PROVIDER_ID,
    TTSVoice,
    get_tts_provider,
)
from openbase_coder_cli.voice_models import VOICE_ENGINE_LIVE

# Operator override of the live engine's voice (read by livekit_agent/config.py
# as LIVE_VOICE_DEFAULT_VOICE); its presence anchors the identity on that voice.
LIVE_VOICE_OVERRIDE_ENV = "LIVEKIT_LIVE_VOICE_VOICE"
# GPT-Live voice for a provider voice that has no pair of its own (local
# Kokoro voices): still the same gender as the chosen voice.
DEFAULT_GPT_LIVE_VOICE_BY_GENDER = {MASCULINE: "cedar"}

IDENTITY_SOURCE_DISPATCHER_VOICE = "dispatcher_voice"
IDENTITY_SOURCE_LIVE_VOICE_OVERRIDE = "live_voice_override"


@dataclass(frozen=True)
class VoiceIdentity:
    provider: str
    # The TTS voice announcements and the pipeline speak with.
    voice_id: str
    voice_name: str
    # The GPT-Live voice a live call speaks with.
    gpt_live_voice: str
    source: str

    def payload(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "voice_id": self.voice_id,
            "voice_name": self.voice_name,
            "gpt_live_voice": self.gpt_live_voice,
            "source": self.source,
        }


def current_voice_identity(config_path: Path | None = None) -> VoiceIdentity:
    configured = dispatcher_voice(config_path)
    provider = get_tts_provider(configured["provider"])
    paired_live_voice = _paired_live_voice(provider.voice_for_id(configured["id"]))
    override = live_voice_override()
    if (
        override is not None
        and override.id != paired_live_voice
        and provider.provider_id in {CARTESIA_PROVIDER_ID, OPENBASE_CLOUD_TTS_PROVIDER_ID}
        and selected_voice_engine(config_path) == VOICE_ENGINE_LIVE
    ):
        match = cartesia_voice_for_gpt_live_voice(override.id)
        return VoiceIdentity(
            provider=provider.provider_id,
            voice_id=match.id,
            voice_name=match.name,
            gpt_live_voice=override.id,
            source=IDENTITY_SOURCE_LIVE_VOICE_OVERRIDE,
        )
    return VoiceIdentity(
        provider=provider.provider_id,
        voice_id=configured["id"],
        voice_name=configured["name"],
        gpt_live_voice=paired_live_voice,
        source=IDENTITY_SOURCE_DISPATCHER_VOICE,
    )


def _paired_live_voice(provider_voice: TTSVoice | None) -> str:
    if provider_voice is None:
        return DEFAULT_GPT_LIVE_VOICE
    if provider_voice.gpt_live_voice:
        return provider_voice.gpt_live_voice
    return DEFAULT_GPT_LIVE_VOICE_BY_GENDER.get(
        provider_voice.gender or "", DEFAULT_GPT_LIVE_VOICE
    )


def live_voice_override() -> GptLiveVoiceCatalogEntry | None:
    """The GPT-Live voice pinned through the environment, if it is one the
    live engine accepts."""
    return gpt_live_voice_for_id(os.getenv(LIVE_VOICE_OVERRIDE_ENV, ""))
