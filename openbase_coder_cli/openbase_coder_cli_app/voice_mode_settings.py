"""Voice mode settings API view.

``dispatcher`` (the default) routes every voice call through the shared
dispatcher thread, which delegates to Super Agents. ``direct`` skips the
dispatcher: each call starts a fresh ordinary Super Agent thread that the user
talks to directly, the way a plain coding thread works. The dispatch skill is
installed for every thread, so a direct thread can still spin up Super Agents
when asked; it just carries no dispatcher role or delegation policy.

The LiveKit agent reads the mode when a call starts, so no restart is needed.
"""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import dispatcher_config

VOICE_MODE_DETAILS = {
    dispatcher_config.VOICE_MODE_DISPATCHER: {
        "label": "Dispatcher",
        "summary": (
            "Calls go to the shared dispatcher thread, which delegates work "
            "to Super Agents and transfers you between them."
        ),
    },
    dispatcher_config.VOICE_MODE_DIRECT: {
        "label": "Direct",
        "summary": (
            "Each call starts a fresh thread you talk to directly, with no "
            "dispatcher in between. Transfers to other threads still work."
        ),
    },
}


class VoiceModeSettingsSerializer(serializers.Serializer):
    voice_mode = serializers.ChoiceField(choices=dispatcher_config.VOICE_MODES)


def _voice_mode_payload(*, changed: bool = False) -> dict:
    return {
        "voice_mode": dispatcher_config.voice_mode(),
        "default": dispatcher_config.DEFAULT_VOICE_MODE,
        "options": [
            {"id": option, **VOICE_MODE_DETAILS[option]}
            for option in dispatcher_config.VOICE_MODES
        ],
        "config_path": str(dispatcher_config.CODEX_DISPATCHER_CONFIG_PATH),
        "config_exists": dispatcher_config.CODEX_DISPATCHER_CONFIG_PATH.is_file(),
        "changed": changed,
        "restart_required": False,
        "applies_hint": "The new mode applies to the next voice call.",
    }


@api_view(["GET", "PUT"])
def voice_mode_settings(request):
    """Read or update how voice calls pick the thread they talk to."""
    if request.method == "GET":
        return Response(_voice_mode_payload(), status=status.HTTP_200_OK)

    serializer = VoiceModeSettingsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    previous = dispatcher_config.voice_mode()
    try:
        dispatcher_config.set_voice_mode(serializer.validated_data["voice_mode"])
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(
        _voice_mode_payload(changed=previous != dispatcher_config.voice_mode()),
        status=status.HTTP_200_OK,
    )
