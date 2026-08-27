"""Backend model settings API views.

The user picks a MODEL per role (dispatcher, default super agent); the model
implies the engine (Claude Code vs Codex), so there is no separate
engine choice. Options span both engines, labeled, with Codex models
unavailable on the Openbase Cloud location (Claude Code is the cloud engine).
"""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import dispatcher_config
from openbase_coder_cli.cli.backend import read_backend, write_backend
from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH


class BackendModelSerializer(serializers.Serializer):
    role = serializers.ChoiceField(
        choices=(
            dispatcher_config.DISPATCHER_MODEL_ROLE,
            dispatcher_config.SUPER_AGENTS_MODEL_ROLE,
        ),
        default=dispatcher_config.SUPER_AGENTS_MODEL_ROLE,
    )
    model = serializers.CharField()


def _role_entry(role: str) -> dict:
    model = dispatcher_config.backend_model(role)
    return {
        "model": model,
        "engine": dispatcher_config.model_engine(model),
    }


def _backend_model_payload(*, changed: bool = False) -> dict:
    configured_backend = read_backend(DEFAULT_ENV_FILE_PATH)
    location = dispatcher_config.backend_location(configured_backend)
    options = dispatcher_config.combined_model_options(location)
    roles = {
        "dispatcher": _role_entry(dispatcher_config.DISPATCHER_MODEL_ROLE),
        "super_agents": _role_entry(dispatcher_config.SUPER_AGENTS_MODEL_ROLE),
    }
    return {
        "backend": configured_backend,
        "location": location,
        "roles": roles,
        # Legacy shape kept for older clients (Electron settings).
        "models": {role: entry["model"] for role, entry in roles.items()},
        "effective": {
            role: entry["model"] or "backend default" for role, entry in roles.items()
        },
        "options": [dict(option) for option in options],
        "allows_custom": False,
        "config_path": str(dispatcher_config.CODEX_DISPATCHER_CONFIG_PATH),
        "changed": changed,
        "restart_required": changed,
        "restart_hint": (
            "Restart or recreate the dispatcher/MCP host for model changes to apply."
        ),
    }


@api_view(["GET", "PUT"])
def backend_model_settings(request):
    """Read or update the dispatcher / default super-agent model choices."""
    if request.method == "GET":
        return Response(_backend_model_payload())

    serializer = BackendModelSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    role = serializer.validated_data["role"]
    model = serializer.validated_data["model"]
    configured_backend = read_backend(DEFAULT_ENV_FILE_PATH)
    location = dispatcher_config.backend_location(configured_backend)
    if not dispatcher_config.is_known_combined_model(model, location):
        allowed = ", ".join(
            option["id"]
            for option in dispatcher_config.combined_model_options(location)
            if option["available"]
        )
        return Response(
            {"error": f"Model must be one of: {allowed}."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    previous = dispatcher_config.backend_model(role)
    try:
        dispatcher_config.set_backend_model(
            role,
            model,
            backend=dispatcher_config.identity_for_model(model, location),
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    if role == dispatcher_config.SUPER_AGENTS_MODEL_ROLE:
        # The default super-agent model decides the primary backend identity
        # (creation default for unlabeled launches) at the current location.
        write_backend(
            DEFAULT_ENV_FILE_PATH,
            dispatcher_config.identity_for_model(model, location),
        )
    return Response(_backend_model_payload(changed=previous != " ".join(model.split())))
