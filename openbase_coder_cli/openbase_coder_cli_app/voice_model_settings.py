"""Voice model settings API views.

The user picks a voice MODEL for calls (GPT-Live or the classic pipeline),
exactly like the agent model: the model implies the engine, so there is no
separate engine choice. GPT-Live always runs through Openbase Cloud; the
STT and TTS provider settings only matter for the classic pipeline.
"""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import dispatcher_config
from openbase_coder_cli.voice_models import (
    DEFAULT_VOICE_MODEL_ID,
    VOICE_ENGINE_PIPELINE,
    voice_model_options_payload,
)

APPLIES_HINT = "The new voice model applies to the next voice call."


class VoiceModelSettingsSerializer(serializers.Serializer):
    model = serializers.CharField()


def _voice_model_payload(*, changed: bool = False) -> dict:
    model = dispatcher_config.selected_voice_model_id()
    engine = dispatcher_config.selected_voice_engine()
    return {
        "model": model,
        "engine": engine,
        "default": DEFAULT_VOICE_MODEL_ID,
        "options": voice_model_options_payload(),
        "pipeline_settings_relevant": engine == VOICE_ENGINE_PIPELINE,
        "config_path": str(dispatcher_config.CODEX_DISPATCHER_CONFIG_PATH),
        "changed": changed,
        "restart_required": False,
        "applies_hint": APPLIES_HINT,
    }


@api_view(["GET", "PUT"])
def voice_model_settings(request):
    """Read or update the voice model used on calls."""
    if request.method == "GET":
        return Response(_voice_model_payload())

    serializer = VoiceModelSettingsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    previous = dispatcher_config.selected_voice_model_id()
    try:
        result = dispatcher_config.set_voice_model(serializer.validated_data["model"])
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(_voice_model_payload(changed=result["model"] != previous))
