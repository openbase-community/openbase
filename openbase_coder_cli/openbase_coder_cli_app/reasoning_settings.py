"""Dispatcher and Super Agents reasoning settings API views.

Reasoning levels only apply to Codex turns. On the Claude Code execution
backend, effort is derived from the service tier (Fast mode), so the setting
is reported as non-editable and writes are rejected.
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

REASONING_EFFORT_OPTIONS = ("low", "medium", "high", "xhigh")
CLAUDE_BACKEND_REASONING_ERROR = dispatcher_config.CLAUDE_BACKEND_REASONING_ERROR


def _reasoning_editable() -> bool:
    try:
        return configured_execution_backend() != CLAUDE_CODE_BACKEND
    except Exception:
        return True


class ReasoningSettingsSerializer(serializers.Serializer):
    dispatcher_reasoning_effort = serializers.ChoiceField(
        choices=REASONING_EFFORT_OPTIONS
    )
    super_agents_reasoning_effort = serializers.ChoiceField(
        choices=REASONING_EFFORT_OPTIONS
    )


def _reasoning_payload(*, changed: bool = False) -> dict:
    dispatcher_effort = dispatcher_config.dispatcher_reasoning_effort()
    super_agents_effort = dispatcher_config.super_agents_reasoning_effort()
    editable = _reasoning_editable()
    return {
        "editable": editable,
        "not_editable_reason": None if editable else CLAUDE_BACKEND_REASONING_ERROR,
        "dispatcher_reasoning_effort": dispatcher_effort,
        "super_agents_reasoning_effort": super_agents_effort,
        "effective": {
            "dispatcher_reasoning_effort": dispatcher_effort or "app-server default",
            "super_agents_reasoning_effort": super_agents_effort or "high",
        },
        "options": list(REASONING_EFFORT_OPTIONS),
        "config_path": str(dispatcher_config.CODEX_DISPATCHER_CONFIG_PATH),
        "config_exists": dispatcher_config.CODEX_DISPATCHER_CONFIG_PATH.is_file(),
        "changed": changed,
        "restart_required": False,
        "restart_hint": "Reasoning changes apply to the next turn.",
    }


@api_view(["GET", "PUT"])
def reasoning_settings(request):
    """Read or update dispatcher and Super Agents reasoning effort."""
    if request.method == "GET":
        return Response(_reasoning_payload())

    if not _reasoning_editable():
        return Response(
            {"error": CLAUDE_BACKEND_REASONING_ERROR},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = ReasoningSettingsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    previous = _reasoning_payload()
    dispatcher_config.set_dispatcher_reasoning_effort(
        serializer.validated_data["dispatcher_reasoning_effort"]
    )
    dispatcher_config.set_super_agents_reasoning_effort(
        serializer.validated_data["super_agents_reasoning_effort"]
    )
    next_payload = _reasoning_payload(
        changed=(
            previous["dispatcher_reasoning_effort"]
            != serializer.validated_data["dispatcher_reasoning_effort"]
            or previous["super_agents_reasoning_effort"]
            != serializer.validated_data["super_agents_reasoning_effort"]
        )
    )
    return Response(next_payload)
