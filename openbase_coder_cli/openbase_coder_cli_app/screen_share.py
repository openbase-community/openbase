"""Phone- and console-initiated screen sharing of this backend's desktop.

``livekit-companion-start/``, ``-stop/`` and ``-status/`` drive the platform's
screen-share companion for the caller's active LiveKit room:

- macOS: the Openbase desktop app's control server, which launches the Swift
  companion (ScreenCaptureKit capture + CGEvent remote control).
- Linux: the Python companion (``openbase-coder computer-use companion``).

Errors carry a stable ``code`` so every client shows the same plain message,
most importantly for the macOS privacy grants the companion needs.
"""

from __future__ import annotations

import logging
import platform
from typing import Any

import click
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import desktop_control
from openbase_coder_cli.livekit_announcer import (
    AnnouncerValidationError,
    NoActiveLiveKitRoomError,
)
from openbase_coder_cli.openbase_coder_cli_app import livekit as _livekit

logger = logging.getLogger(__name__)

SCREEN_RECORDING_PERMISSION_CODE = "screen_recording_permission_required"
SCREEN_RECORDING_PERMISSION_DETAIL = (
    "Allow Screen Recording for OpenbaseScreenShareCompanion on the Mac "
    "(System Settings > Privacy & Security > Screen & System Audio Recording), "
    "then try again."
)
DESKTOP_APP_UNAVAILABLE_CODE = "desktop_app_unavailable"
DESKTOP_APP_UNAVAILABLE_DETAIL = (
    "The Openbase desktop app is not running on this computer. Open Openbase "
    "on the Mac, then try again."
)
# Desktop builds before the coded companion errors report the raw
# ScreenCaptureKit text for a missing Screen Recording grant.
# Desktop builds whose Electron drops the companion's ``code`` still pass its
# message through, so match that too.
_LEGACY_SCREEN_RECORDING_MARKERS = (
    "declined TCC",
    "Screen Recording permission",
    "Allow Screen Recording",
)


def _companion_client_factory():
    from openbase_coder_cli.cli.computer_use import CompanionClient

    return CompanionClient()


def _require_jwt_caller(request) -> None:
    if not isinstance(request.auth, dict):
        raise serializers.ValidationError(
            {"detail": "A JWT-authenticated caller is required to share the screen."}
        )


def _platform() -> str:
    system = platform.system()
    return {"Darwin": "macos", "Linux": "linux"}.get(system, system.lower())


def _desktop_error_response(exc: desktop_control.DesktopControlError) -> Response:
    message = str(exc)
    if exc.code == SCREEN_RECORDING_PERMISSION_CODE or any(
        marker in message for marker in _LEGACY_SCREEN_RECORDING_MARKERS
    ):
        return Response(
            {
                "detail": SCREEN_RECORDING_PERMISSION_DETAIL,
                "code": SCREEN_RECORDING_PERMISSION_CODE,
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    if exc.unreachable:
        return Response(
            {
                "detail": DESKTOP_APP_UNAVAILABLE_DETAIL,
                "code": DESKTOP_APP_UNAVAILABLE_CODE,
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response(
        {
            "detail": f"Unable to start screen sharing: {message}",
            "code": exc.code or "companion_error",
        },
        status=status.HTTP_502_BAD_GATEWAY,
    )


def _start_macos(payload: dict[str, str]) -> dict[str, Any]:
    return desktop_control.request(
        "POST",
        "/livekit-companion/start-screen-share",
        json={
            "roomUrl": payload["roomUrl"],
            "companionToken": payload["companionToken"],
            "companionTokenExpiresAt": payload["companionTokenExpiresAt"],
        },
        # Covers a cold companion launch (up to 10 s) plus the room join.
        timeout=45,
    )


def _start_linux(payload: dict[str, str]) -> dict[str, Any]:
    client = _companion_client_factory()
    client.ensure_running()
    return client.start_screen_share(
        {
            "roomUrl": payload["roomUrl"],
            "token": payload["companionToken"],
            "identity": _livekit.LIVEKIT_COMPANION_IDENTITY,
            "name": _livekit.LIVEKIT_COMPANION_NAME,
            "companionTokenExpiresAt": payload["companionTokenExpiresAt"],
        }
    )


@api_view(["POST"])
def livekit_companion_start(request):
    """Start sharing this computer's screen into the caller's LiveKit room."""
    input_serializer = _livekit.LiveKitCompanionSessionSerializer(data=request.data)
    input_serializer.is_valid(raise_exception=True)
    _require_jwt_caller(request)

    host = _platform()
    if host not in ("macos", "linux"):
        return Response(
            {
                "supported": False,
                "started": False,
                "platform": host,
                "detail": "Screen sharing is available on macOS and Linux computers.",
            }
        )

    room_name = input_serializer.validated_data.get("room_name") or None
    try:
        payload = _livekit._build_companion_session_payload(
            room_name=room_name,
            require_active_target=not bool(room_name),
        )
        companion = _start_macos(payload) if host == "macos" else _start_linux(payload)
    except serializers.ValidationError:
        raise
    except AnnouncerValidationError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except NoActiveLiveKitRoomError as exc:
        return Response(
            {"detail": str(exc), "code": "no_active_room"},
            status=status.HTTP_404_NOT_FOUND,
        )
    except desktop_control.DesktopControlError as exc:
        logger.warning("Desktop screen share start failed: %s (code=%s)", exc, exc.code)
        return _desktop_error_response(exc)
    except Exception as exc:
        logger.exception("Unable to start LiveKit companion")
        return Response(
            {
                "detail": f"Unable to start screen sharing: {exc}",
                "code": "companion_error",
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(
        {
            "supported": True,
            "started": True,
            "platform": host,
            "roomName": payload["roomName"],
            "companion": companion,
        }
    )


@api_view(["POST"])
def livekit_companion_stop(request):
    """Stop sharing this computer's screen. Idempotent: an absent companion is stopped."""
    _require_jwt_caller(request)
    host = _platform()
    try:
        if host == "macos":
            companion = desktop_control.request(
                "POST", "/livekit-companion/stop-screen-share", json={}, launch=False
            )
        elif host == "linux":
            companion = _companion_client_factory().stop_screen_share()
        else:
            return Response({"supported": False, "stopped": False, "platform": host})
    except desktop_control.DesktopControlError as exc:
        if not exc.unreachable:
            return _desktop_error_response(exc)
        companion = {"state": "off"}
    except click.ClickException:
        # Linux companion not running: nothing is being shared.
        companion = {"state": "off"}
    return Response(
        {"supported": True, "stopped": True, "platform": host, "companion": companion}
    )


@api_view(["GET"])
def livekit_companion_status(request):
    """Report companion state and, on macOS, the privacy grants it needs."""
    _require_jwt_caller(request)
    host = _platform()
    body: dict[str, Any] = {"supported": host in ("macos", "linux"), "platform": host}
    if host == "macos":
        try:
            response = desktop_control.request("GET", "/status", launch=False)
        except desktop_control.DesktopControlError:
            return Response(
                {
                    **body,
                    "state": "unavailable",
                    "code": DESKTOP_APP_UNAVAILABLE_CODE,
                    "detail": DESKTOP_APP_UNAVAILABLE_DETAIL,
                }
            )
        companion = response.get("companion") if isinstance(response, dict) else None
        companion = companion if isinstance(companion, dict) else {}
        body["state"] = companion.get("state") or "off"
        for key in ("screenRecordingGranted", "accessibilityGranted"):
            if isinstance(companion.get(key), bool):
                body[key] = companion[key]
    elif host == "linux":
        try:
            companion = _companion_client_factory().status()
        except click.ClickException:
            companion = {"state": "off"}
        body["state"] = companion.get("state") or "off"
        if companion.get("error"):
            body["detail"] = companion["error"]
    return Response(body)
