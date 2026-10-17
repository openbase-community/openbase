"""Voice model settings API views.

The user picks a voice MODEL for calls (GPT-Live or the classic pipeline),
exactly like the agent model: the model implies the engine, so there is no
separate engine choice. GPT-Live always runs through Openbase Cloud; the
STT and TTS provider settings only matter for the classic pipeline.
"""

from __future__ import annotations

from hashlib import sha256

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import dispatcher_config
from openbase_coder_cli.config.cloud_audio import (
    LIVE_VOICE_LOGIN_REQUIRED_HINT,
    LiveVoiceAvailability,
    OpenbaseCloudAudioSubscriptionError,
    audio_usage_summary,
    live_voice_availability_from_usage,
)
from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
)
from openbase_coder_cli.services.onboarding import web_backend_url
from openbase_coder_cli.voice_models import (
    DEFAULT_VOICE_MODEL_ID,
    VOICE_ENGINE_PIPELINE,
    voice_model_options_payload,
)

APPLIES_HINT = "The new voice model applies to the next voice call."


@api_view(["GET"])
def voice_model_usage(request):
    """The GPT-Live budget and whether GPT-Live can be used right now.

    An unavailable budget must never prevent changing the voice model. Cloud
    is polled only while GPT-Live is selected, or while the runtime switched
    the install to Classic on its own: then the user is waiting to hear that
    GPT-Live is available again. An install that chose Classic itself is left
    alone.
    """
    model = dispatcher_config.selected_voice_model_id()
    auto_switch = dispatcher_config.voice_model_auto_switch()
    if (
        dispatcher_config.selected_voice_engine() == VOICE_ENGINE_PIPELINE
        and auto_switch is None
    ):
        return Response({"model": model, "budget": None, "live_voice": None})
    cloud_url = web_backend_url().rstrip("/")
    try:
        usage = audio_usage_summary(cloud_url)
    except AuthLoginRequiredError:
        return Response(
            {
                "model": model,
                "budget": None,
                "live_voice": LiveVoiceAvailability(
                    False, "login_required", LIVE_VOICE_LOGIN_REQUIRED_HINT
                ).payload(),
            }
        )
    except (AuthTransientError, OpenbaseCloudAudioSubscriptionError):
        return Response({"model": model, "budget": None, "live_voice": None})
    live_voice = live_voice_availability_from_usage(usage).payload()
    # Older Cloud versions do not report the enforced shared/lifetime budget.
    # Do not guess from provider-only spend: it can understate actual usage.
    budget = usage.get("live_voice_budget")
    if isinstance(budget, dict) and budget.get("warning_id"):
        budget = {
            **budget,
            "warning_id": sha256(
                f"{cloud_url}:{budget['warning_id']}".encode()
            ).hexdigest(),
        }
    else:
        budget = None
    return Response({"model": model, "budget": budget, "live_voice": live_voice})


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
        # Set while the runtime moved this install to Classic on its own
        # (GPT-Live credits ran out); an explicit choice clears it.
        "auto_switch": dispatcher_config.voice_model_auto_switch(),
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
