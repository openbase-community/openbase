"""Stable dispatcher and agent identities, mapped to each voice engine.

The dispatcher follows its configured provider voice and optional GPT-Live
pin. Each agent retains its own assigned provider voice and catalog mapping;
that dispatcher-only pin never collapses the roster into a single voice.
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
        and selected_voice_engine(config_path) == VOICE_ENGINE_LIVE
    ):
        voice_id = configured["id"]
        voice_name = configured["name"]
        if provider.provider_id in {
            CARTESIA_PROVIDER_ID,
            OPENBASE_CLOUD_TTS_PROVIDER_ID,
        }:
            match = cartesia_voice_for_gpt_live_voice(override.id)
            voice_id = match.id
            voice_name = match.name
        return VoiceIdentity(
            provider=provider.provider_id,
            voice_id=voice_id,
            voice_name=voice_name,
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


def agent_voice_identity(voice_id: str, *, provider_id: str | None = None) -> VoiceIdentity:
    """Map an assigned agent voice without applying the dispatcher override."""
    from openbase_coder_cli.dispatcher_config import selected_tts_provider_id

    provider = get_tts_provider(provider_id or selected_tts_provider_id())
    voice = provider.voice_for_id(voice_id)
    if voice is None:
        raise ValueError(f"Unknown agent voice: {voice_id}")
    return VoiceIdentity(
        provider=provider.provider_id,
        voice_id=voice.id,
        voice_name=voice.name,
        gpt_live_voice=_paired_live_voice(voice),
        source="agent_voice",
    )


def route_voice_identity(router) -> VoiceIdentity:
    if router.is_dispatcher_active:
        return current_voice_identity()
    if not router.active_target_voice_id:
        raise ValueError("Active agent has no assigned voice")
    return agent_voice_identity(router.active_target_voice_id)
