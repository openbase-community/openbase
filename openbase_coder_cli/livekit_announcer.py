from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import livekit.api as livekit_api

ANNOUNCER_TOPIC = "openbase.announcer.say"
MAX_ANNOUNCER_TEXT_LENGTH = 2000
AUDIO_PLAYBACK_KIND = "audio_file"
SUPPORTED_AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".aac", ".ogg")
logger = logging.getLogger(__name__)


class AnnouncerError(Exception):
    """Base error for announcer publishing failures."""


class AnnouncerValidationError(AnnouncerError):
    """The requested announcer message is invalid."""


class NoActiveLiveKitRoomError(AnnouncerError):
    """No target LiveKit room can receive announcer messages."""


@dataclass(frozen=True)
class AnnouncerPublishResult:
    message_id: str
    room_name: str
    agent_identities: tuple[str, ...]


def validate_announcer_text(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        raise AnnouncerValidationError("text is required")
    if len(normalized) > MAX_ANNOUNCER_TEXT_LENGTH:
        raise AnnouncerValidationError(
            f"text must be {MAX_ANNOUNCER_TEXT_LENGTH} characters or fewer"
        )
    return normalized


def _transient_connection_errors() -> tuple[type[BaseException], ...]:
    import aiohttp

    return (aiohttp.ClientConnectionError, ConnectionError, asyncio.TimeoutError)


async def _run_with_livekit_client(
    operation,
    livekit_client: livekit_api.LiveKitAPI | None,
):
    """Run ``operation(client)``, retrying once on transient connection loss.

    Only retries when this call owns the client (built it itself); a caller
    supplied client is used as-is.
    """
    if livekit_client is not None:
        return await operation(livekit_client)
    client = _build_livekit_client()
    try:
        try:
            return await operation(client)
        except _transient_connection_errors() as exc:
            logger.warning(
                "announcer_transient_connection_error retrying error=%s",
                exc,
            )
    finally:
        await client.aclose()
    client = _build_livekit_client()
    try:
        return await operation(client)
    finally:
        await client.aclose()


async def publish_announcer_message(
    text: str,
    *,
    room_name: str | None = None,
    voice_id: str | None = None,
    livekit_client: livekit_api.LiveKitAPI | None = None,
) -> AnnouncerPublishResult:
    normalized_text = validate_announcer_text(text)

    async def operation(client) -> AnnouncerPublishResult:
        return await _publish_announcer_message_with(
            client, normalized_text, room_name=room_name, voice_id=voice_id
        )

    return await _run_with_livekit_client(operation, livekit_client)


async def _publish_announcer_message_with(
    client: livekit_api.LiveKitAPI,
    normalized_text: str,
    *,
    room_name: str | None,
    voice_id: str | None,
) -> AnnouncerPublishResult:
    import livekit.api as livekit_api

    started = time.monotonic()
    logger.info(
        "dispatch_timing stage=announcer_publish_request room_name=%s text_len=%d",
        room_name or "",
        len(normalized_text),
    )
    target = await _resolve_target_room(client, room_name=room_name)
    message_id = f"announcer-{uuid.uuid4().hex}"
    logger.info(
        "dispatch_timing stage=announcer_target_resolved message_id=%s "
        "room_name=%s agent_count=%d elapsed_ms=%d",
        message_id,
        target.room_name,
        len(target.agent_identities),
        int((time.monotonic() - started) * 1000),
    )
    payload = {
        "message_id": message_id,
        "text": normalized_text,
    }
    target_voice_id = _safe_announcer_voice_id(
        (voice_id or "").strip() or _active_target_voice_id()
    )
    if target_voice_id:
        payload["voice_id"] = target_voice_id
    await client.room.send_data(
        livekit_api.SendDataRequest(
            room=target.room_name,
            data=json.dumps(payload).encode("utf-8"),
            kind=livekit_api.DataPacket.Kind.RELIABLE,
            destination_identities=list(target.agent_identities),
            topic=ANNOUNCER_TOPIC,
        )
    )
    logger.info(
        "dispatch_timing stage=announcer_send_data_end message_id=%s "
        "room_name=%s elapsed_ms=%d",
        message_id,
        target.room_name,
        int((time.monotonic() - started) * 1000),
    )
    return AnnouncerPublishResult(
        message_id=message_id,
        room_name=target.room_name,
        agent_identities=target.agent_identities,
    )


async def publish_announcer_audio_file(
    audio_path: str,
    *,
    room_name: str | None = None,
    livekit_client: livekit_api.LiveKitAPI | None = None,
) -> AnnouncerPublishResult:
    normalized_path = str(audio_path).strip()
    if not normalized_path:
        raise AnnouncerValidationError("audio_path is required")

    async def operation(client) -> AnnouncerPublishResult:
        return await _publish_announcer_audio_file_with(
            client, normalized_path, room_name=room_name
        )

    return await _run_with_livekit_client(operation, livekit_client)


async def _publish_announcer_audio_file_with(
    client: livekit_api.LiveKitAPI,
    normalized_path: str,
    *,
    room_name: str | None,
) -> AnnouncerPublishResult:
    import livekit.api as livekit_api

    started = time.monotonic()
    logger.info(
        "dispatch_timing stage=announcer_audio_publish_request room_name=%s",
        room_name or "",
    )
    target = await _resolve_target_room(client, room_name=room_name)
    message_id = f"announcer-audio-{uuid.uuid4().hex}"
    payload = {
        "kind": AUDIO_PLAYBACK_KIND,
        "message_id": message_id,
        "audio_path": normalized_path,
    }
    await client.room.send_data(
        livekit_api.SendDataRequest(
            room=target.room_name,
            data=json.dumps(payload).encode("utf-8"),
            kind=livekit_api.DataPacket.Kind.RELIABLE,
            destination_identities=list(target.agent_identities),
            topic=ANNOUNCER_TOPIC,
        )
    )
    logger.info(
        "dispatch_timing stage=announcer_audio_send_data_end message_id=%s "
        "room_name=%s elapsed_ms=%d",
        message_id,
        target.room_name,
        int((time.monotonic() - started) * 1000),
    )
    return AnnouncerPublishResult(
        message_id=message_id,
        room_name=target.room_name,
        agent_identities=target.agent_identities,
    )


def _active_target_voice_id() -> str | None:
    from openbase_coder_cli.livekit_voice_route import get_livekit_voice_route_state

    state = get_livekit_voice_route_state()
    return state.active_target_voice_id if state.active_target_thread_id else None


def _safe_announcer_voice_id(voice_id: str | None) -> str | None:
    if not voice_id:
        return None
    from openbase_coder_cli.dispatcher_config import selected_tts_provider_id
    from openbase_coder_cli.tts_providers import KOKORO_PROVIDER_ID, get_tts_provider

    try:
        provider = get_tts_provider(selected_tts_provider_id())
    except ValueError:
        return voice_id
    if provider.provider_id != KOKORO_PROVIDER_ID:
        return voice_id
    if provider.super_agent_voice_for_id(voice_id):
        return voice_id
    fallback = provider.default_announcer_voice().id
    logger.warning(
        "announcer_voice_fallback provider=%s requested_voice_id=%s fallback_voice_id=%s",
        provider.provider_id,
        voice_id,
        fallback,
    )
    return fallback


@dataclass(frozen=True)
class _TargetRoom:
    room_name: str
    agent_identities: tuple[str, ...]


async def active_voice_room_exists() -> bool:
    """True when a live voice session (agent + user in a room) is active."""

    async def operation(client) -> bool:
        try:
            await _resolve_target_room(client, room_name=None)
        except NoActiveLiveKitRoomError:
            return False
        return True

    return await _run_with_livekit_client(operation, None)


def _build_livekit_client() -> livekit_api.LiveKitAPI:
    import livekit.api as livekit_api

    api_key = os.environ.get("LIVEKIT_API_KEY")
    api_secret = os.environ.get("LIVEKIT_API_SECRET")
    if not api_key or not api_secret:
        raise AnnouncerValidationError("Local LiveKit credentials are not configured.")
    return livekit_api.LiveKitAPI(
        url=_livekit_api_url(),
        api_key=api_key,
        api_secret=api_secret,
    )


def _livekit_api_url() -> str:
    """Server-API URL for twirp calls from this host.

    LIVEKIT_URL is the client-facing signaling URL; in tailscale mode it is
    the tailnet node IP, which livekit-server (bound to 127.0.0.1) does not
    listen on from this host — every announcer publish then dies with a
    connection reset. Same-host API calls must target loopback instead.
    """
    explicit = os.environ.get("LIVEKIT_API_URL", "").strip()
    if explicit:
        return explicit
    url = os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
    node_ip = os.environ.get("LIVEKIT_NODE_IP", "").strip()
    if node_ip and f"//{node_ip}:" in url:
        return url.replace(f"//{node_ip}:", "//localhost:", 1)
    if node_ip and url.endswith(f"//{node_ip}"):
        return url.replace(f"//{node_ip}", "//localhost", 1)
    return url


async def _resolve_target_room(
    client: livekit_api.LiveKitAPI,
    *,
    room_name: str | None,
) -> _TargetRoom:
    import livekit.api as livekit_api

    if room_name:
        agent_identities = await _agent_identities_for_room(client, room_name)
        if not agent_identities:
            raise NoActiveLiveKitRoomError(
                f"No active LiveKit agent participant found in room {room_name!r}."
            )
        return _TargetRoom(room_name=room_name, agent_identities=agent_identities)

    response = await client.room.list_rooms(livekit_api.ListRoomsRequest())
    rooms = sorted(
        response.rooms,
        key=lambda room: int(
            getattr(room, "creation_time_ms", 0)
            or getattr(room, "creation_time", 0)
            or 0
        ),
        reverse=True,
    )
    for room in rooms:
        if int(getattr(room, "num_participants", 0) or 0) <= 0:
            continue
        participant_response = await client.room.list_participants(
            livekit_api.ListParticipantsRequest(room=room.name)
        )
        agent_identities = _active_agent_identities(participant_response.participants)
        has_user = any(
            _is_active_standard_participant(p)
            for p in participant_response.participants
        )
        if agent_identities and has_user:
            return _TargetRoom(
                room_name=room.name,
                agent_identities=agent_identities,
            )

    raise NoActiveLiveKitRoomError("No active LiveKit voice room was found.")


async def _agent_identities_for_room(
    client: livekit_api.LiveKitAPI,
    room_name: str,
) -> tuple[str, ...]:
    import livekit.api as livekit_api

    participant_response = await client.room.list_participants(
        livekit_api.ListParticipantsRequest(room=room_name)
    )
    return _active_agent_identities(participant_response.participants)


def _active_agent_identities(participants) -> tuple[str, ...]:
    return tuple(
        participant.identity
        for participant in participants
        if _is_active_agent_participant(participant)
    )


def _is_active_agent_participant(participant) -> bool:
    import livekit.api as livekit_api

    return (
        int(getattr(participant, "kind", -1)) == livekit_api.ParticipantInfo.Kind.AGENT
        and _is_connected_participant(participant)
        and bool(getattr(participant, "identity", ""))
    )


def _is_active_standard_participant(participant) -> bool:
    import livekit.api as livekit_api

    return int(
        getattr(participant, "kind", -1)
    ) == livekit_api.ParticipantInfo.Kind.STANDARD and _is_connected_participant(
        participant
    )


def _is_connected_participant(participant) -> bool:
    import livekit.api as livekit_api

    return int(getattr(participant, "state", -1)) in {
        livekit_api.ParticipantInfo.State.JOINED,
        livekit_api.ParticipantInfo.State.ACTIVE,
    }
