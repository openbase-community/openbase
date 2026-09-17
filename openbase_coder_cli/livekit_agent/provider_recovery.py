"""Bound incoming retries without letting short outages terminate outbound speech."""
import logging

from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.agents.types import APIConnectOptions
from openbase_coder_cli.livekit_agent.tts_selection import TTS_CONNECT_OPTIONS

logger = logging.getLogger(__name__)


def voice_connect_options():
    options = SessionConnectOptions(
        stt_conn_options=APIConnectOptions(timeout=60, max_retry=10),
        tts_conn_options=TTS_CONNECT_OPTIONS,
    )
    logger.info("dispatch_timing stage=voice_provider_connection_options "
        "stt_timeout_seconds=%s stt_max_retry=%s tts_timeout_seconds=%s",
        options.stt_conn_options.timeout, options.stt_conn_options.max_retry,
        options.tts_conn_options.timeout)
    return options
