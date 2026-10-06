"""Voice model settings API views.

The user picks a voice MODEL for calls (GPT-Live or the classic pipeline),
exactly like the agent model: the model implies the engine, so there is no
separate engine choice. When GPT-Live is selected a secondary choice says
where it comes from (Openbase Cloud or the user's own OpenAI key); the STT
and TTS provider settings only matter for the classic pipeline.
"""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import dispatcher_config
from openbase_coder_cli.voice_models import (
    DEFAULT_VOICE_MODEL_ID,
    VOICE_ENGINE_PIPELINE,
    live_voice_provider_options_payload,
    normalize_live_voice_provider_id,
    normalize_voice_model_id,
    voice_model_options_payload,
)

APPLIES_HINT = "The new voice model applies to the next voice call."


class VoiceModelSettingsSerializer(serializers.Serializer):
    model = serializers.CharField(required=False, allow_blank=False)
    live_voice_provider = serializers.CharField(required=False, allow_blank=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Provide a voice model, a live voice provider, or both."
            )
        return attrs


def _voice_model_payload(*, changed: bool = False) -> dict:
    model = dispatcher_config.selected_voice_model_id()
    engine = dispatcher_config.selected_voice_engine()
    return {
        "model": model,
        "engine": engine,
        "default": DEFAULT_VOICE_MODEL_ID,
        "options": voice_model_options_payload(),
        "live_voice_provider": dispatcher_config.selected_live_voice_provider_id(),
        "live_voice_provider_options": live_voice_provider_options_payload(),
        "pipeline_settings_relevant": engine == VOICE_ENGINE_PIPELINE,
        "config_path": str(dispatcher_config.CODEX_DISPATCHER_CONFIG_PATH),
        "changed": changed,
        "restart_required": False,
        "applies_hint": APPLIES_HINT,
    }


@api_view(["GET", "PUT"])
def voice_model_settings(request):
    """Read or update the voice model and the GPT-Live provider."""
    if request.method == "GET":
        return Response(_voice_model_payload())

    serializer = VoiceModelSettingsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    model = serializer.validated_data.get("model")
    live_voice_provider = serializer.validated_data.get("live_voice_provider")

    # Validate everything before writing anything, so a bad provider never
    # leaves a half-applied model change behind.
    try:
        if model is not None:
            normalize_voice_model_id(model)
        if live_voice_provider is not None:
            normalize_live_voice_provider_id(live_voice_provider)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    previous = _voice_model_payload()
    if model is not None:
        dispatcher_config.set_voice_model(model)
    if live_voice_provider is not None:
        dispatcher_config.set_live_voice_provider(live_voice_provider)

    current = _voice_model_payload()
    changed = (
        current["model"] != previous["model"]
        or current["live_voice_provider"] != previous["live_voice_provider"]
    )
    return Response(_voice_model_payload(changed=changed))
