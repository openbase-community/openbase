"""Agent service tier settings API views.

Fast mode is scoped: the voice dispatcher defaults to the fast lane
(latency-sensitive), Super Agents default to standard. Either scope can be
switched in console settings — including fast for both.

Service tiers only apply to Codex turns. On the Claude Code execution
backend the setting is reported as non-editable and writes are rejected;
Claude turns always run at the standard tier.
"""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli import dispatcher_config
from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    configured_execution_backend,
)
from openbase_coder_cli.cli.setup import _upsert_env_file_values
from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH

SERVICE_TIER_OPTIONS = ("fast", "standard")
SERVICE_TIER_DETAILS = {
    "fast": {
        "label": "Fast",
        "summary": "Use the faster Codex service tier.",
    },
    "standard": {
        "label": "Standard",
        "summary": "Use the standard Codex service tier.",
    },
}
CLAUDE_BACKEND_SERVICE_TIER_ERROR = dispatcher_config.CLAUDE_BACKEND_SERVICE_TIER_ERROR


def _service_tier_editable() -> bool:
    try:
        return configured_execution_backend() != CLAUDE_CODE_BACKEND
    except Exception:
        return True


class ServiceTierSettingsSerializer(serializers.Serializer):
    dispatcher_service_tier = serializers.ChoiceField(
        choices=SERVICE_TIER_OPTIONS, required=False
    )
    super_agents_service_tier = serializers.ChoiceField(
        choices=SERVICE_TIER_OPTIONS, required=False
    )


def _service_tier_payload(*, changed: bool = False) -> dict:
    dispatcher_tier = dispatcher_config.dispatcher_service_tier()
    super_agents_tier = dispatcher_config.super_agents_service_tier()
    editable = _service_tier_editable()
    return {
        "editable": editable,
        "not_editable_reason": (
            None if editable else CLAUDE_BACKEND_SERVICE_TIER_ERROR
        ),
        "dispatcher_service_tier": dispatcher_tier,
        "super_agents_service_tier": super_agents_tier,
        "effective": {
            "dispatcher_service_tier": dispatcher_tier,
            "super_agents_service_tier": super_agents_tier,
        },
        "defaults": {
            "dispatcher_service_tier": dispatcher_config.DEFAULT_DISPATCHER_SERVICE_TIER,
            "super_agents_service_tier": (
                dispatcher_config.DEFAULT_SUPER_AGENTS_SERVICE_TIER
            ),
        },
        "options": [
            {"id": option, **SERVICE_TIER_DETAILS[option]}
            for option in SERVICE_TIER_OPTIONS
        ],
        "config_path": str(dispatcher_config.CODEX_DISPATCHER_CONFIG_PATH),
        "config_exists": dispatcher_config.CODEX_DISPATCHER_CONFIG_PATH.is_file(),
        "env_file_exists": DEFAULT_ENV_FILE_PATH.is_file(),
        "changed": changed,
        "restart_required": changed,
        "restart_hint": (
            "Restart Openbase services for Codex app-server defaults to pick up "
            "the change. New LiveKit turns use the selected tiers after the "
            "voice agent restarts."
        ),
    }


@api_view(["GET", "PUT"])
def service_tier_settings(request):
    """Read or update the per-scope service tiers used for new turns."""
    if request.method == "GET":
        return Response(_service_tier_payload(), status=status.HTTP_200_OK)

    if not _service_tier_editable():
        return Response(
            {"error": CLAUDE_BACKEND_SERVICE_TIER_ERROR},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = ServiceTierSettingsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    if not data:
        return Response(
            {"error": "Provide dispatcher_service_tier or super_agents_service_tier."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    previous = (
        dispatcher_config.dispatcher_service_tier(),
        dispatcher_config.super_agents_service_tier(),
    )
    try:
        if "dispatcher_service_tier" in data:
            dispatcher_config.set_dispatcher_service_tier(
                data["dispatcher_service_tier"]
            )
        if "super_agents_service_tier" in data:
            dispatcher_config.set_super_agents_service_tier(
                data["super_agents_service_tier"]
            )
        DEFAULT_ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # The codex-app-server's ambient default (turns without an explicit
        # tier) follows the Super Agents lane; dispatcher turns always carry
        # their tier explicitly.
        _upsert_env_file_values(
            DEFAULT_ENV_FILE_PATH,
            {
                "CODEX_SERVICE_TIER": dispatcher_config.super_agents_service_tier(),
                "DISPATCHER_SERVICE_TIER": dispatcher_config.dispatcher_service_tier(),
                "SUPER_AGENTS_SERVICE_TIER": (
                    dispatcher_config.super_agents_service_tier()
                ),
            },
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    current = (
        dispatcher_config.dispatcher_service_tier(),
        dispatcher_config.super_agents_service_tier(),
    )
    return Response(
        _service_tier_payload(changed=previous != current),
        status=status.HTTP_200_OK,
    )
