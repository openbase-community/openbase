from __future__ import annotations

import asyncio
import logging
import time

import click

from openbase_coder_cli.livekit_announcer import (
    AnnouncerError,
    NoActiveLiveKitRoomError,
    publish_announcer_audio_file,
)
from openbase_coder_cli.paths import OPENBASE_SOUNDS_DIR
from openbase_coder_cli.services.definitions import ServiceDefinition

VOICE_INTERRUPTING_SERVICE_NAMES = frozenset(
    {"livekit-agent", "livekit-server", "openbase-voice-turn"}
)
VOICE_INTERRUPTING_ACTIONS = frozenset({"stop", "restart"})
VOICE_WARNING_SOUND_NAME = "deactivate"
VOICE_WARNING_DELAY_SECONDS = 1.2

logger = logging.getLogger(__name__)


def service_action_interrupts_voice(service: ServiceDefinition, action: str) -> bool:
    return (
        service.name in VOICE_INTERRUPTING_SERVICE_NAMES
        and action in VOICE_INTERRUPTING_ACTIONS
    )


def any_service_action_interrupts_voice(
    services: list[ServiceDefinition] | tuple[ServiceDefinition, ...],
    action: str,
) -> bool:
    return any(service_action_interrupts_voice(service, action) for service in services)


def warn_before_voice_interruption(
    *,
    reason: str,
    emit_cli_warning: bool = True,
    delay_seconds: float = VOICE_WARNING_DELAY_SECONDS,
) -> bool:
    """Play a local warning sound before interrupting LiveKit voice services."""
    sound_path = OPENBASE_SOUNDS_DIR / f"{VOICE_WARNING_SOUND_NAME}.wav"
    if not sound_path.is_file():
        _warn(f"Voice warning sound not found: {sound_path}", emit_cli_warning)
        return False

    try:
        asyncio.run(publish_announcer_audio_file(str(sound_path)))
    except NoActiveLiveKitRoomError:
        logger.info("No active LiveKit voice room for interruption warning: %s", reason)
        return False
    except AnnouncerError as exc:
        _warn(f"Unable to play voice interruption warning: {exc}", emit_cli_warning)
        return False
    except Exception as exc:
        logger.warning("Unable to play voice interruption warning", exc_info=True)
        _warn(f"Unable to play voice interruption warning: {exc}", emit_cli_warning)
        return False

    if delay_seconds > 0:
        time.sleep(delay_seconds)
    return True


def _warn(message: str, emit_cli_warning: bool) -> None:
    logger.warning(message)
    if emit_cli_warning:
        click.echo(f"Warning: {message}", err=True)
