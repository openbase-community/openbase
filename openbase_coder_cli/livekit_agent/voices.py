"""Compatibility facade for LiveKit agent voice helpers.

Thread-id-to-Super-Agent voice mapping is owned by livekit_voice_route.
"""

from pathlib import Path

from openbase_coder_cli import livekit_voice_route as _voice_route
from openbase_coder_cli.dispatcher_config import dispatcher_voice
from openbase_coder_cli.livekit_agent.config import LIVEKIT_DISPATCHER_CONFIG_PATH
from openbase_coder_cli.tts_providers import CARTESIA_PROVIDER_ID

CartesiaVoice = _voice_route.CartesiaVoice
SUPER_AGENT_VOICE_IDS = _voice_route.SUPER_AGENT_VOICE_IDS
SUPER_AGENT_VOICES = _voice_route.SUPER_AGENT_VOICES
_current_super_agent_voices = _voice_route._current_super_agent_voices
_voices_from_ids = _voice_route._voices_from_ids
stable_super_agent_voice = _voice_route.stable_super_agent_voice
stable_super_agent_voice_id = _voice_route.stable_super_agent_voice_id


def dispatcher_voice_config(
    *,
    config_path: str | Path | None = None,
) -> CartesiaVoice:
    configured = dispatcher_voice(
        Path(config_path or LIVEKIT_DISPATCHER_CONFIG_PATH).expanduser()
    )
    return CartesiaVoice(
        voice_id=configured["id"],
        name=configured["name"],
        provider=configured.get("provider", CARTESIA_PROVIDER_ID),
    )
