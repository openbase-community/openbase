"""Parsing of announcer and voice-route data packets from the LiveKit room."""

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass

from livekit import rtc

from openbase_coder_cli.livekit_agent.config import (
    AGENT_STATUS_TOPIC,
    ANNOUNCER_AUDIO_KIND,
    ANNOUNCER_TOPIC,
    VOICE_LIFECYCLE_TOPIC,
    VOICE_ROUTE_TOPIC,
)
from openbase_coder_cli.livekit_agent.voice_delivery import VoiceDeliveryRecord

logger = logging.getLogger(__name__)

VOICE_LIFECYCLE_PROTOCOL_VERSION = 2


async def publish_agent_error_packet(
    room: rtc.Room,
    *,
    code: str,
    detail: str,
) -> str:
    """Publish an agent error status packet so room participants can show it.

    Client contract (additive; old clients ignore unknown topics): a reliable
    data packet on topic ``openbase.agent.status`` whose payload is JSON:
    ``{"type": "agent_error", "code": <machine-readable code>,
    "detail": <human-readable message>, "message_id": <unique id>}``.
    """
    message_id = f"agent-status-{uuid.uuid4().hex}"
    payload = {
        "type": "agent_error",
        "code": code,
        "detail": detail,
        "message_id": message_id,
    }
    await room.local_participant.publish_data(
        json.dumps(payload).encode("utf-8"),
        reliable=True,
        topic=AGENT_STATUS_TOPIC,
    )
    logger.error(
        "dispatch_timing stage=agent_error_packet_published message_id=%s "
        "code=%s detail=%r",
        message_id,
        code,
        detail,
    )
    return message_id


async def publish_voice_lifecycle_packet(
    room: rtc.Room,
    *,
    event: str,
    record: VoiceDeliveryRecord,
    reason: str = "",
) -> str:
    """Publish a voice lifecycle packet for clients with explicit voice UI.

    Client contract (additive; old clients ignore unknown topics): a reliable
    data packet on topic ``openbase.voice.lifecycle`` whose payload is JSON:
    ``{"type": "voice_lifecycle", "event": <event>, ...}``.
    """
    packet_id = f"voice-lifecycle-{uuid.uuid4().hex}"
    route = record.route_at_acceptance
    payload = {
        "type": "voice_lifecycle",
        "protocol_version": VOICE_LIFECYCLE_PROTOCOL_VERSION,
        "event": event,
        "packet_id": packet_id,
        "delivery_id": record.delivery_id,
        "message_id": record.message_id,
        "turn_id": record.turn_id or "",
        "status": record.status,
        "created_at_unix_ms": int(time.time() * 1000),
        "room": _json_string(record.room_name),
        "room_id": _json_string(record.room_id),
        "route": {
            "version": route.route_version,
            "thread_id": _json_string(route.active_thread_id),
            "kind": _json_string(route.active_route),
            "voice_id": _json_string(route.active_voice_id),
            "voice_name": _json_string(route.active_voice_name),
        },
        "prompt_hash": record.prompt_hash,
        "prompt_len": record.prompt_len,
        "speech_hash": record.speech_hash,
        "speech_len": record.speech_len,
        "tts_text_hash": record.tts_text_hash,
        "tts_text_len": record.tts_text_len,
        "audio_events": record.audio_events,
        "audio_seconds": record.audio_seconds,
        "reason": reason,
    }
    if record.user_turn_closure_source:
        payload["user_turn"] = {
            "confidence": record.user_turn_closure_confidence,
            "source": record.user_turn_closure_source,
            "delay_ms": record.user_turn_closure_delay_ms,
            "eou_probability": record.user_turn_eou_probability,
            "silence_ms": record.user_turn_silence_ms,
            "transcript_confidence": record.user_turn_transcript_confidence,
            "transcription_delay_ms": record.user_turn_transcription_delay_ms,
            "transcript_len": record.prompt_len,
            "text_hash": record.prompt_hash,
            "completion_reason": record.user_turn_completion_reason,
        }
    await room.local_participant.publish_data(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        reliable=True,
        topic=VOICE_LIFECYCLE_TOPIC,
    )
    logger.info(
        "dispatch_timing stage=voice_lifecycle_packet_published event=%s "
        "packet_id=%s delivery_id=%s status=%s route_version=%d "
        "route_thread_id=%s room=%s reason=%s",
        event,
        packet_id,
        record.delivery_id,
        record.status,
        route.route_version,
        route.active_thread_id,
        record.room_name,
        reason,
    )
    return packet_id


@dataclass(frozen=True)
class AnnouncerMessage:
    message_id: str
    text: str
    voice_id: str | None = None


@dataclass(frozen=True)
class AnnouncerAudioMessage:
    message_id: str
    audio_path: str


AnnouncerQueueItem = AnnouncerMessage | AnnouncerAudioMessage


@dataclass(frozen=True)
class QueuedAnnouncerItem:
    message: AnnouncerQueueItem
    enqueued_at: float


@dataclass(frozen=True)
class VoiceRouteCommand:
    action: str
    thread_id: str | None = None
    cwd: str | None = None
    label: str | None = None
    active_target_voice_id: str | None = None
    active_target_voice_name: str | None = None


def _packet_json_payload(
    data_packet: rtc.DataPacket,
    *,
    topic: str,
    label: str,
) -> dict | None:
    if data_packet.topic != topic:
        return None

    try:
        payload = json.loads(data_packet.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning(
            "dispatch_timing stage=data_packet_malformed label=%s topic=%s "
            "payload_bytes=%d payload_hash=%s",
            label,
            data_packet.topic,
            len(data_packet.data),
            _packet_hash(data_packet),
        )
        return None

    if not isinstance(payload, dict):
        logger.warning(
            "dispatch_timing stage=data_packet_unexpected_payload label=%s topic=%s "
            "payload_type=%s payload_bytes=%d payload_hash=%s",
            label,
            data_packet.topic,
            type(payload).__name__,
            len(data_packet.data),
            _packet_hash(data_packet),
        )
        return None

    return payload


def parse_announcer_packet(data_packet: rtc.DataPacket) -> AnnouncerMessage | None:
    payload = _packet_json_payload(
        data_packet,
        topic=ANNOUNCER_TOPIC,
        label="announcer",
    )
    if payload is None:
        return None
    if payload.get("kind") == ANNOUNCER_AUDIO_KIND:
        return None

    text = str(payload.get("text") or "").strip()
    if not text:
        logger.warning(
            "dispatch_timing stage=announcer_packet_missing_text topic=%s "
            "payload_bytes=%d payload_hash=%s",
            data_packet.topic,
            len(data_packet.data),
            _packet_hash(data_packet),
        )
        return None

    message_id = str(payload.get("message_id") or f"announcer-{uuid.uuid4().hex}")
    return AnnouncerMessage(
        message_id=message_id,
        text=text,
        voice_id=_optional_packet_str(payload.get("voice_id")),
    )


def parse_announcer_audio_packet(
    data_packet: rtc.DataPacket,
) -> AnnouncerAudioMessage | None:
    payload = _packet_json_payload(
        data_packet,
        topic=ANNOUNCER_TOPIC,
        label="announcer audio",
    )
    if payload is None or payload.get("kind") != ANNOUNCER_AUDIO_KIND:
        return None

    audio_path = str(payload.get("audio_path") or "").strip()
    if not audio_path:
        logger.warning(
            "dispatch_timing stage=announcer_audio_packet_missing_path topic=%s "
            "payload_bytes=%d payload_hash=%s",
            data_packet.topic,
            len(data_packet.data),
            _packet_hash(data_packet),
        )
        return None

    message_id = str(payload.get("message_id") or f"announcer-audio-{uuid.uuid4().hex}")
    return AnnouncerAudioMessage(
        message_id=message_id,
        audio_path=audio_path,
    )


def _packet_participant_identity(data_packet: rtc.DataPacket) -> str:
    participant = getattr(data_packet, "participant", None)
    return str(getattr(participant, "identity", "") or "")


def _packet_hash(data_packet: rtc.DataPacket) -> str:
    return hashlib.sha256(data_packet.data).hexdigest()[:12]


def _json_string(value) -> str:
    return value if isinstance(value, str) else str(value or "")


def parse_voice_route_packet(data_packet: rtc.DataPacket) -> VoiceRouteCommand | None:
    payload = _packet_json_payload(
        data_packet,
        topic=VOICE_ROUTE_TOPIC,
        label="voice route",
    )
    if payload is None:
        return None

    action = str(payload.get("action") or "").strip()
    if not action:
        return None
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    return VoiceRouteCommand(
        action=action,
        thread_id=_optional_packet_str(payload.get("thread_id")),
        cwd=_optional_packet_str(payload.get("cwd")),
        label=_optional_packet_str(payload.get("label")),
        active_target_voice_id=_optional_packet_str(
            state.get("active_target_voice_id")
        ),
        active_target_voice_name=_optional_packet_str(
            state.get("active_target_voice_name")
        )
        or _optional_packet_str(payload.get("agent_name")),
    )


def _optional_packet_str(value) -> str | None:
    return value if isinstance(value, str) and value else None
