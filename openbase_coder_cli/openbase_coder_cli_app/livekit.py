"""LiveKit voice, routing, and token API views."""

from __future__ import annotations

import json
import logging
import os
import platform
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import livekit.api as livekit_api
from asgiref.sync import async_to_sync
from django.conf import settings
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.config.cloud_audio import (
    OpenbaseCloudAudioSubscriptionError,
    ensure_openbase_cloud_audio_subscription,
    openbase_cloud_subscription_entitlement,
)
from openbase_coder_cli.config.token_manager import (
    DEFAULT_WEB_BACKEND_URL,
    AuthLoginRequiredError,
    AuthTransientError,
)
from openbase_coder_cli.dispatcher_config import (
    dispatcher_voice,
    selected_stt_provider_id,
    selected_tts_provider_id,
    set_dispatcher_voice,
    set_stt_provider,
    set_tts_provider_and_dispatcher_voice,
)
from openbase_coder_cli.livekit_announcer import (
    MAX_ANNOUNCER_TEXT_LENGTH,
    SUPPORTED_AUDIO_EXTENSIONS,
    AnnouncerError,
    AnnouncerValidationError,
    NoActiveLiveKitRoomError,
    _build_livekit_client,
    _resolve_target_room,
    publish_announcer_audio_file,
    publish_announcer_message,
)
from openbase_coder_cli.livekit_voice_history import (
    UnknownAgentVoiceError,
    get_voice_history_entry_for_agent_name,
    voice_history_debug_snapshot,
)
from openbase_coder_cli.livekit_voice_route import (
    VoiceRouteBlockedError,
    VoiceRouteError,
    get_livekit_voice_route_state,
    is_dispatcher_identity,
    live_target_transfer_blocker,
    publish_exit_to_dispatch,
    publish_transfer_to_thread,
    super_agent_voice_for_context,
)
from openbase_coder_cli.mcp.session_manager import get_session_manager
from openbase_coder_cli.openbase_coder_cli_app.common import _request_identity
from openbase_coder_cli.services.cloud_workspace import cloud_workspace_id
from openbase_coder_cli.stt_providers import (
    LOCAL_MLX_WHISPER_STT_PROVIDER_ID,
    download_local_mlx_whisper,
    local_mlx_whisper_readiness,
    stt_provider_options_payload,
)
from openbase_coder_cli.tts_providers import (
    KOKORO_PROVIDER_ID,
    all_tts_providers,
    get_tts_provider,
)

logger = logging.getLogger(__name__)

LIVEKIT_COMPANION_IDENTITY = "openbase-screen-share-companion"
LIVEKIT_COMPANION_NAME = "Openbase Screen Share"
LIVEKIT_COMPANION_TOKEN_TTL = timedelta(hours=1)
LIVEKIT_CLIENT_API_KEY_ENV = "LIVEKIT_CLIENT_API_KEY"
LIVEKIT_CLIENT_API_SECRET_ENV = "LIVEKIT_CLIENT_API_SECRET"


class LiveKitRoomTokenSerializer(serializers.Serializer):
    room_name = serializers.CharField(required=False, allow_blank=True)
    livekit_dispatch_agent_name = serializers.CharField()


class LiveKitCompanionSessionSerializer(serializers.Serializer):
    room_name = serializers.CharField(required=False, allow_blank=True)


class AnnouncerSaySerializer(serializers.Serializer):
    agent_name = serializers.CharField(
        trim_whitespace=True,
        max_length=256,
    )
    text = serializers.CharField(
        trim_whitespace=True,
        max_length=MAX_ANNOUNCER_TEXT_LENGTH,
    )
    room_name = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )


class AnnouncerPlaySerializer(serializers.Serializer):
    audio_path = serializers.CharField(
        trim_whitespace=True,
        max_length=4096,
    )
    room_name = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )

    def validate_audio_path(self, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_file():
            raise serializers.ValidationError("Audio file not found.")
        if path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            supported = ", ".join(SUPPORTED_AUDIO_EXTENSIONS)
            raise serializers.ValidationError(
                f"Unsupported audio file extension. Supported: {supported}."
            )
        return str(path.resolve())


def _livekit_client_token_credentials() -> tuple[str, str]:
    api_key = os.environ.get(LIVEKIT_CLIENT_API_KEY_ENV, "").strip()
    api_secret = os.environ.get(LIVEKIT_CLIENT_API_SECRET_ENV, "").strip()
    if not api_key or not api_secret:
        raise serializers.ValidationError(
            {
                "detail": (
                    "Local LiveKit client token credentials are not configured. "
                    "Run 'openbase-coder setup' to generate "
                    "LIVEKIT_CLIENT_API_KEY and LIVEKIT_CLIENT_API_SECRET, then "
                    "restart the Openbase Coder services."
                )
            }
        )

    server_api_key = os.environ.get("LIVEKIT_API_KEY", "").strip()
    server_api_secret = os.environ.get("LIVEKIT_API_SECRET", "").strip()
    if api_key == server_api_key or api_secret == server_api_secret:
        raise serializers.ValidationError(
            {
                "detail": (
                    "Client-facing LiveKit token credentials must be separate "
                    "from the local server credentials. Run 'openbase-coder setup' "
                    "to regenerate them, then restart the Openbase Coder services."
                )
            }
        )
    return api_key, api_secret


class VoiceRouteCommandSerializer(serializers.Serializer):
    room_name = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )


class VoiceRouteTransferSerializer(VoiceRouteCommandSerializer):
    thread_id = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    agent_name = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    label = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )

    def validate(self, attrs):
        if not attrs.get("thread_id") and not attrs.get("agent_name"):
            raise serializers.ValidationError("thread_id or agent_name is required.")
        return attrs


class DispatcherVoiceSerializer(serializers.Serializer):
    voice_id = serializers.CharField(
        trim_whitespace=True,
        max_length=256,
    )


class TTSSettingsSerializer(DispatcherVoiceSerializer):
    provider = serializers.CharField(
        trim_whitespace=True,
        max_length=32,
    )


class STTSettingsSerializer(serializers.Serializer):
    provider = serializers.CharField(
        trim_whitespace=True,
        max_length=32,
    )


@api_view(["POST"])
def user_say(request):
    """Publish an announcer message into the active LiveKit voice room."""
    started = time.monotonic()
    input_serializer = AnnouncerSaySerializer(data=request.data)
    input_serializer.is_valid(raise_exception=True)

    room_name = input_serializer.validated_data.get("room_name") or None
    agent_name = input_serializer.validated_data["agent_name"]
    logger.info(
        "dispatch_timing stage=user_say_request room_name=%s agent_name=%s text_len=%d",
        room_name or "",
        agent_name,
        len(input_serializer.validated_data["text"]),
    )
    try:
        voice_entry = get_voice_history_entry_for_agent_name(agent_name)
        logger.info(
            "dispatch_timing stage=user_say_voice_resolved agent_name=%s "
            "thread_id=%s voice_id=%s voice_name=%s source=%s",
            agent_name,
            voice_entry.thread_id,
            voice_entry.voice_id or "",
            voice_entry.voice_name or "",
            voice_entry.source,
        )
        result = async_to_sync(publish_announcer_message)(
            input_serializer.validated_data["text"],
            room_name=room_name,
            voice_id=voice_entry.voice_id,
        )
    except UnknownAgentVoiceError as exc:
        catalog_voice = get_tts_provider(selected_tts_provider_id()).voice_for_name(
            agent_name
        )
        logger.warning(
            "dispatch_timing stage=user_say_voice_unknown agent_name=%s "
            "catalog_voice_id=%s catalog_voice_name=%s history=%s",
            agent_name,
            catalog_voice.id if catalog_voice else "",
            catalog_voice.name if catalog_voice else "",
            voice_history_debug_snapshot(agent_name),
        )
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    except AnnouncerValidationError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except NoActiveLiveKitRoomError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    except AnnouncerError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        logger.exception("Unable to publish LiveKit announcer message")
        return Response(
            {"detail": "Unable to publish announcer message."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    response = Response(
        {
            "message_id": result.message_id,
            "room_name": result.room_name,
            "status": "published",
        },
        status=status.HTTP_202_ACCEPTED,
    )
    logger.info(
        "dispatch_timing stage=user_say_response message_id=%s room_name=%s elapsed_ms=%d",
        result.message_id,
        result.room_name,
        int((time.monotonic() - started) * 1000),
    )
    return response


@api_view(["POST"])
def user_play(request):
    """Publish an audio playback message into the active LiveKit voice room."""
    started = time.monotonic()
    input_serializer = AnnouncerPlaySerializer(data=request.data)
    input_serializer.is_valid(raise_exception=True)

    room_name = input_serializer.validated_data.get("room_name") or None
    audio_path = input_serializer.validated_data["audio_path"]
    logger.info(
        "dispatch_timing stage=user_play_request room_name=%s audio_basename=%s",
        room_name or "",
        Path(audio_path).name,
    )
    try:
        result = async_to_sync(publish_announcer_audio_file)(
            audio_path,
            room_name=room_name,
        )
    except AnnouncerValidationError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except NoActiveLiveKitRoomError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    except AnnouncerError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        logger.exception("Unable to publish LiveKit audio playback message")
        return Response(
            {"detail": "Unable to publish audio playback message."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    response = Response(
        {
            "message_id": result.message_id,
            "room_name": result.room_name,
            "status": "published",
        },
        status=status.HTTP_202_ACCEPTED,
    )
    logger.info(
        "dispatch_timing stage=user_play_response message_id=%s room_name=%s elapsed_ms=%d",
        result.message_id,
        result.room_name,
        int((time.monotonic() - started) * 1000),
    )
    return response


@api_view(["GET"])
def cartesia_voice_settings(request):
    """Compatibility alias for the generic TTS settings response."""
    return Response(_tts_settings_payload(), status=status.HTTP_200_OK)


@api_view(["GET", "PUT"])
def tts_settings(request):
    """Return or persist the configured TTS provider and dispatcher voice."""
    if request.method == "GET":
        return Response(_tts_settings_payload(), status=status.HTTP_200_OK)

    input_serializer = TTSSettingsSerializer(data=request.data)
    input_serializer.is_valid(raise_exception=True)
    try:
        selected_voice = set_tts_provider_and_dispatcher_voice(
            provider_id=input_serializer.validated_data["provider"],
            voice_id=input_serializer.validated_data["voice_id"],
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        {
            **_tts_settings_payload(),
            "dispatcher_voice": selected_voice,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
def kokoro_tts_download(request):
    """Download/cache the local Kokoro model and every supported voice file."""
    provider = get_tts_provider(KOKORO_PROVIDER_ID)
    try:
        download = provider.download_all_voices()
    except Exception as exc:
        return Response(
            {
                "local_download": {
                    "provider": KOKORO_PROVIDER_ID,
                    "ready": False,
                    "required_files": 0,
                    "cached_files": 0,
                    "detail": str(exc),
                }
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return Response(
        {
            **_tts_settings_payload(),
            "local_download": download.payload(),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET", "PUT"])
def stt_settings(request):
    """Return or persist the configured speech-to-text provider."""
    if request.method == "GET":
        return Response(_stt_settings_payload(), status=status.HTTP_200_OK)

    input_serializer = STTSettingsSerializer(data=request.data)
    input_serializer.is_valid(raise_exception=True)
    try:
        selected = set_stt_provider(input_serializer.validated_data["provider"])
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        {
            **_stt_settings_payload(),
            **selected,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
def local_stt_download(request):
    """Download/cache the local MLX Whisper STT model."""
    try:
        download = download_local_mlx_whisper()
    except Exception as exc:
        return Response(
            {
                "local_download": {
                    "provider": LOCAL_MLX_WHISPER_STT_PROVIDER_ID,
                    "ready": False,
                    "model": "",
                    "detail": str(exc),
                }
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return Response(
        {
            **_stt_settings_payload(),
            "local_download": download.payload(),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["PUT"])
def dispatcher_voice_settings(request):
    """Persist the dispatcher voice selected from the current TTS provider catalog."""
    input_serializer = DispatcherVoiceSerializer(data=request.data)
    input_serializer.is_valid(raise_exception=True)
    try:
        selected_voice = set_dispatcher_voice(
            input_serializer.validated_data["voice_id"]
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        {
            "dispatcher_voice": selected_voice,
        },
        status=status.HTTP_200_OK,
    )


def _tts_settings_payload() -> dict:
    provider_id = selected_tts_provider_id()
    provider = get_tts_provider(provider_id)
    return {
        "provider": provider_id,
        "providers": [
            {
                "id": candidate.provider_id,
                "name": candidate.display_name,
                "local": candidate.provider_id == KOKORO_PROVIDER_ID,
            }
            for candidate in all_tts_providers()
        ],
        "voices": provider.catalog_payload(),
        "voices_by_provider": {
            candidate.provider_id: candidate.catalog_payload()
            for candidate in all_tts_providers()
        },
        "dispatcher_voice": dispatcher_voice(),
        "local_download": get_tts_provider(KOKORO_PROVIDER_ID).readiness().payload(),
    }


def _stt_settings_payload() -> dict:
    return {
        "provider": selected_stt_provider_id(),
        "providers": stt_provider_options_payload(),
        "local_download": local_mlx_whisper_readiness().payload(),
    }


@api_view(["GET"])
def livekit_voice_route(request):
    """Return the current LiveKit voice route state."""
    state = get_livekit_voice_route_state()
    return Response(
        {
            "dispatcher_thread_id": state.dispatcher_thread_id,
            "dispatcher_voice_id": state.dispatcher_voice_id,
            "dispatcher_voice_name": state.dispatcher_voice_name,
            "active_target_thread_id": state.active_target_thread_id,
            "active_target_kind": state.active_target_kind,
            "active_target_label": state.active_target_label,
            "active_target_voice_id": state.active_target_voice_id,
            "active_target_voice_name": state.active_target_voice_name,
            "active_route": state.active_route,
            "instruction_override_supported": state.instruction_override_supported,
            "blocked_reason": live_target_transfer_blocker(),
            "updated_at": state.updated_at,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
def livekit_voice_route_exit(request):
    """Route future LiveKit voice turns back to the dispatcher."""
    input_serializer = VoiceRouteCommandSerializer(data=request.data)
    input_serializer.is_valid(raise_exception=True)

    room_name = input_serializer.validated_data.get("room_name") or None
    try:
        result = async_to_sync(publish_exit_to_dispatch)(room_name=room_name)
    except AnnouncerValidationError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except NoActiveLiveKitRoomError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    except VoiceRouteError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        logger.exception("Unable to publish LiveKit voice route exit")
        return Response(
            {"detail": "Unable to publish LiveKit voice route command."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(
        {
            "command_id": result.command_id,
            "room_name": result.room_name,
            "status": "published",
            "state": {
                "dispatcher_thread_id": result.state.dispatcher_thread_id,
                "dispatcher_voice_id": result.state.dispatcher_voice_id,
                "dispatcher_voice_name": result.state.dispatcher_voice_name,
                "active_target_thread_id": result.state.active_target_thread_id,
                "active_target_voice_id": result.state.active_target_voice_id,
                "active_target_voice_name": result.state.active_target_voice_name,
                "active_route": result.state.active_route,
            },
        },
        status=status.HTTP_202_ACCEPTED,
    )


@api_view(["POST"])
def livekit_voice_route_transfer(request):
    """Route future LiveKit voice turns to an existing Codex thread if safe."""
    input_serializer = VoiceRouteTransferSerializer(data=request.data)
    input_serializer.is_valid(raise_exception=True)

    manager = get_session_manager()
    resolved = async_to_sync(_resolve_voice_transfer_target)(
        manager,
        thread_id=input_serializer.validated_data.get("thread_id") or None,
        agent_name=input_serializer.validated_data.get("agent_name") or None,
    )
    if resolved["status"] != 200:
        return Response(resolved["data"], status=resolved["status"])

    thread = resolved["thread"]
    thread_id = thread.session_id
    label = input_serializer.validated_data.get("label") or _thread_label(thread)
    requested_agent_name = input_serializer.validated_data.get("agent_name") or None
    agent_name = (
        requested_agent_name
        or getattr(thread, "agent_name", None)
        or _derived_thread_agent_name(thread)
        or None
    )

    transfer_kwargs = {
        "directory": thread.directory,
        "label": label,
        "room_name": input_serializer.validated_data.get("room_name") or None,
    }
    if agent_name:
        transfer_kwargs["agent_name"] = agent_name

    try:
        result = async_to_sync(publish_transfer_to_thread)(thread_id, **transfer_kwargs)
    except VoiceRouteBlockedError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
    except AnnouncerValidationError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except NoActiveLiveKitRoomError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    except VoiceRouteError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        logger.exception("Unable to publish LiveKit voice route transfer")
        return Response(
            {"detail": "Unable to publish LiveKit voice route command."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(
        {
            "command_id": result.command_id,
            "room_name": result.room_name,
            "status": "published",
            "state": {
                "dispatcher_thread_id": result.state.dispatcher_thread_id,
                "dispatcher_voice_id": result.state.dispatcher_voice_id,
                "dispatcher_voice_name": result.state.dispatcher_voice_name,
                "active_target_thread_id": result.state.active_target_thread_id,
                "active_target_voice_id": result.state.active_target_voice_id,
                "active_target_voice_name": result.state.active_target_voice_name,
                "active_route": result.state.active_route,
            },
        },
        status=status.HTTP_202_ACCEPTED,
    )


async def _resolve_voice_transfer_target(
    manager,
    *,
    thread_id: str | None,
    agent_name: str | None,
) -> dict:
    route_state = get_livekit_voice_route_state()
    if thread_id:
        thread = await manager.get_thread_state(thread_id)
        if thread is None:
            return {
                "status": status.HTTP_404_NOT_FOUND,
                "data": {"detail": f"Thread {thread_id} not found."},
            }
        if _is_dispatcher_transfer_target(thread, route_state):
            return {
                "status": status.HTTP_409_CONFLICT,
                "data": {
                    "detail": (
                        "The dispatcher thread cannot be used as a Super Agent "
                        "voice transfer target. Use exit-to-dispatch to return "
                        "to the dispatcher."
                    )
                },
            }
        return {"status": status.HTTP_200_OK, "thread": thread}

    assert agent_name is not None
    requested_name = _normalize_agent_name(agent_name)
    threads = [
        thread
        for thread in await manager.list_threads()
        if not _is_dispatcher_transfer_target(thread, route_state)
    ]
    exact_matches = [
        thread
        for thread in threads
        if _normalize_agent_name(_thread_agent_name(thread)) == requested_name
    ]
    matches = exact_matches or [
        thread
        for thread in threads
        if _normalize_agent_name(_derived_thread_agent_name(thread)) == requested_name
    ]
    if not matches:
        return {
            "status": status.HTTP_404_NOT_FOUND,
            "data": {"detail": f"Agent {agent_name!r} not found."},
        }
    selected = max(matches, key=_thread_recency_key)
    if len(matches) > 1:
        logger.info(
            "dispatch_timing stage=voice_route_agent_multiple agent_name=%s "
            "selected_thread_id=%s candidates=%s",
            agent_name,
            getattr(selected, "session_id", ""),
            [
                {
                    "thread_id": getattr(thread, "session_id", ""),
                    "name": _thread_agent_name(thread),
                    "updated_at": str(getattr(thread, "updated_at", "") or ""),
                }
                for thread in matches[:10]
            ],
        )
    return {"status": status.HTTP_200_OK, "thread": selected}


def _is_dispatcher_transfer_target(thread, route_state) -> bool:
    thread_id = getattr(thread, "session_id", None)
    return is_dispatcher_identity(
        thread_id if isinstance(thread_id, str) else None,
        _thread_label(thread),
        _thread_agent_name(thread),
        route_state=route_state,
    )


def _thread_agent_name(thread) -> str | None:
    value = getattr(thread, "agent_name", None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _derived_thread_agent_name(thread) -> str | None:
    label = _thread_label(thread)
    if not label:
        return None
    voice = super_agent_voice_for_context(label, label)
    return voice.name if voice else None


def _thread_label(thread) -> str | None:
    for value in (
        getattr(thread, "name", None),
        getattr(thread, "title", None),
        getattr(thread, "preview", None),
        getattr(thread, "directory", None),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_agent_name(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _thread_recency_key(thread) -> tuple[float, str]:
    thread_id = getattr(thread, "session_id", "")
    thread_id_value = thread_id if isinstance(thread_id, str) else ""
    return (
        _thread_recency_timestamp_value(thread, thread_id_value),
        thread_id_value,
    )


def _thread_recency_timestamp_value(thread, thread_id: str) -> float:
    return max(
        _timestamp_value(getattr(thread, "updated_at", None)),
        _timestamp_value(getattr(thread, "created_at", None)),
        _thread_id_timestamp_value(thread_id),
    )


def _thread_id_timestamp_value(thread_id: str) -> float:
    if not thread_id:
        return 0.0
    try:
        parsed = uuid.UUID(thread_id)
    except ValueError:
        return 0.0
    if parsed.version != 7:
        return 0.0
    return int(parsed.hex[:12], 16) / 1000.0


def _timestamp_value(value) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


@api_view(["POST"])
def livekit_room_token(request):
    """Mint a local LiveKit room token for the authenticated caller."""
    input_serializer = LiveKitRoomTokenSerializer(data=request.data)
    input_serializer.is_valid(raise_exception=True)

    api_key, api_secret = _livekit_client_token_credentials()

    if not isinstance(request.auth, dict):
        raise serializers.ValidationError(
            {
                "detail": "A JWT-authenticated caller is required to start a LiveKit session."
            }
        )

    try:
        ensure_openbase_cloud_audio_subscription(
            tts_provider_id=selected_tts_provider_id(),
            stt_provider_id=selected_stt_provider_id(),
            web_backend_url=(
                getattr(settings, "WEB_BACKEND_URL", "") or DEFAULT_WEB_BACKEND_URL
            ).rstrip("/"),
        )
    except AuthLoginRequiredError as exc:
        return Response(
            {"detail": str(exc), "code": "login_required"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    except OpenbaseCloudAudioSubscriptionError as exc:
        return Response(
            {"detail": str(exc), "code": "subscription_required"},
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )
    except AuthTransientError as exc:
        return Response(
            {"detail": str(exc), "code": "cloud_unavailable"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    room_name = input_serializer.validated_data.get("room_name")
    if not room_name:
        room_name = f"room-{uuid.uuid4().hex[:12]}"

    livekit_dispatch_agent_name = input_serializer.validated_data[
        "livekit_dispatch_agent_name"
    ]
    identity = _request_identity(request)

    metadata = {
        "user_identity": identity,
    }

    display_name = (
        request.user.get_full_name().strip()
        if hasattr(request.user, "get_full_name")
        else ""
    )
    if not display_name:
        display_name = identity

    token = (
        livekit_api.AccessToken(api_key=api_key, api_secret=api_secret)
        .with_identity(identity)
        .with_name(display_name)
        .with_grants(
            livekit_api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_room_config(
            livekit_api.RoomConfiguration(
                agents=[
                    livekit_api.RoomAgentDispatch(
                        agent_name=livekit_dispatch_agent_name,
                        metadata=json.dumps(metadata),
                    )
                ]
            )
        )
        .with_ttl(timedelta(hours=1))
        .to_jwt()
    )

    payload: dict[str, Any] = {"token": token, "room_name": room_name}
    workspace_id = cloud_workspace_id()
    if workspace_id:
        payload["workspace"] = {
            "kind": "openbase_cloud",
            "id": workspace_id,
        }
    return Response(payload)


@api_view(["GET"])
def apple_music_playback_entitlement(request):
    """Return whether the caller can use paid Apple Music playback."""
    try:
        entitlement = openbase_cloud_subscription_entitlement(
            web_backend_url=(
                getattr(settings, "WEB_BACKEND_URL", "") or DEFAULT_WEB_BACKEND_URL
            ).rstrip("/"),
            access_token=_request_bearer_jwt(request),
        )
    except AuthLoginRequiredError as exc:
        return Response(
            {"detail": str(exc), "code": "login_required"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    except OpenbaseCloudAudioSubscriptionError as exc:
        return Response(
            {"detail": str(exc), "code": "subscription_required"},
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )
    except AuthTransientError as exc:
        return Response(
            {"detail": str(exc), "code": "cloud_unavailable"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    has_active_subscription = bool(entitlement["has_active_subscription"])
    payload = {
        "feature": "apple_music_playback",
        "available": has_active_subscription,
        "requires_subscription": True,
        "detail": entitlement["detail"],
    }
    if has_active_subscription:
        return Response(payload, status=status.HTTP_200_OK)
    return Response(
        {**payload, "code": "subscription_required"},
        status=status.HTTP_402_PAYMENT_REQUIRED,
    )


def _request_bearer_jwt(request) -> str | None:
    authorization = request.META.get("HTTP_AUTHORIZATION", "")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].casefold() != "bearer":
        return None
    token = parts[1].strip()
    return token if token.count(".") == 2 else None


async def _resolve_companion_target_room(room_name: str | None):
    client = _build_livekit_client()
    try:
        return await _resolve_target_room(client, room_name=room_name)
    finally:
        await client.aclose()


def _build_companion_session_payload(
    *,
    room_name: str | None,
    require_active_target: bool,
) -> dict[str, str]:
    api_key, api_secret = _livekit_client_token_credentials()

    target_room_name = room_name
    if require_active_target or not target_room_name:
        target = async_to_sync(_resolve_companion_target_room)(room_name)
        target_room_name = target.room_name

    expires_at = timezone.now() + LIVEKIT_COMPANION_TOKEN_TTL
    token = (
        livekit_api.AccessToken(api_key=api_key, api_secret=api_secret)
        .with_identity(LIVEKIT_COMPANION_IDENTITY)
        .with_name(LIVEKIT_COMPANION_NAME)
        .with_grants(
            livekit_api.VideoGrants(
                room_join=True,
                room=target_room_name,
                can_publish=True,
                can_subscribe=False,
                can_publish_data=True,
            )
        )
        .with_ttl(LIVEKIT_COMPANION_TOKEN_TTL)
        .to_jwt()
    )

    return {
        "roomUrl": os.environ.get("LIVEKIT_URL", "ws://localhost:7880"),
        "roomName": target_room_name,
        "companionToken": token,
        "companionTokenExpiresAt": expires_at.isoformat(),
    }


def _companion_client_factory():
    from openbase_coder_cli.cli.computer_use import CompanionClient

    return CompanionClient()


@api_view(["GET", "POST"])
def livekit_companion_session(request):
    """Mint a screen-share companion token for the current LiveKit voice room."""
    input_data = request.query_params if request.method == "GET" else request.data
    input_serializer = LiveKitCompanionSessionSerializer(data=input_data)
    input_serializer.is_valid(raise_exception=True)

    if not isinstance(request.auth, dict):
        raise serializers.ValidationError(
            {"detail": "A JWT-authenticated caller is required to share the screen."}
        )

    room_name = input_serializer.validated_data.get("room_name") or None
    try:
        payload = _build_companion_session_payload(
            room_name=room_name,
            require_active_target=True,
        )
    except serializers.ValidationError:
        raise
    except AnnouncerValidationError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except NoActiveLiveKitRoomError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    except Exception:
        logger.exception("Unable to resolve LiveKit companion room")
        return Response(
            {"detail": "Unable to resolve the active LiveKit room."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(payload)


@api_view(["POST"])
def livekit_companion_start(request):
    """Start the Linux screen-share companion for the current LiveKit room."""
    input_serializer = LiveKitCompanionSessionSerializer(data=request.data)
    input_serializer.is_valid(raise_exception=True)

    if not isinstance(request.auth, dict):
        raise serializers.ValidationError(
            {"detail": "A JWT-authenticated caller is required to share the screen."}
        )

    if platform.system() != "Linux":
        return Response(
            {
                "supported": False,
                "started": False,
                "detail": "The Linux screen-share companion is only available on Linux.",
            }
        )

    room_name = input_serializer.validated_data.get("room_name") or None
    try:
        payload = _build_companion_session_payload(
            room_name=room_name,
            require_active_target=not bool(room_name),
        )
        client = _companion_client_factory()
        client.ensure_running()
        companion_response = client.start_screen_share(
            {
                "roomUrl": payload["roomUrl"],
                "token": payload["companionToken"],
                "identity": LIVEKIT_COMPANION_IDENTITY,
                "name": LIVEKIT_COMPANION_NAME,
                "companionTokenExpiresAt": payload["companionTokenExpiresAt"],
            }
        )
    except serializers.ValidationError:
        raise
    except AnnouncerValidationError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except NoActiveLiveKitRoomError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
    except Exception as exc:
        logger.exception("Unable to start LiveKit companion")
        return Response(
            {"detail": f"Unable to start the Linux screen-share companion: {exc}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(
        {
            "supported": True,
            "started": True,
            "roomName": payload["roomName"],
            "companion": companion_response,
        }
    )
