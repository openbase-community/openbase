from __future__ import annotations

from django.conf import settings

if not settings.configured:
    settings.configure(
        DEFAULT_CHARSET="utf-8",
        INSTALLED_APPS=[],
        REST_FRAMEWORK={},
        USE_I18N=False,
    )

from openbase_coder_cli.openbase_coder_cli_app.routines import RoutineCreateSerializer


def test_command_routine_create_serializer_accepts_command_without_prompt() -> None:
    serializer = RoutineCreateSerializer(
        data={
            "name": "discover-prs",
            "kind": "command",
            "command": "generate-workspace-report --workspace .",
            "scheduleType": "interval",
        }
    )

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["intervalSeconds"] == 60


def test_agent_routine_create_serializer_requires_prompt() -> None:
    serializer = RoutineCreateSerializer(
        data={
            "name": "daily-check",
            "kind": "agent",
            "time": "09:00",
        }
    )

    assert not serializer.is_valid()
    assert "prompt" in serializer.errors


def test_trigger_create_serializer_validates_filters() -> None:
    from openbase_coder_cli.openbase_coder_cli_app.routines import (
        TriggerCreateSerializer,
    )

    serializer = TriggerCreateSerializer(
        data={
            "description": "GitHub PR comments",
            "senderPath": "sender.id",
            "senderAllowlist": ["12345"],
            "filters": [
                {"path": "comment.body", "op": "startsWith", "value": "/openbase"}
            ],
        }
    )
    assert serializer.is_valid(), serializer.errors

    bad = TriggerCreateSerializer(
        data={"filters": [{"path": "comment.body", "op": "sounds-like"}]}
    )
    assert not bad.is_valid()


def test_emit_serializer_accepts_payload() -> None:
    from openbase_coder_cli.openbase_coder_cli_app.routines import RoutineEmitSerializer

    serializer = RoutineEmitSerializer(
        data={"payload": {"note": "manual"}, "eventId": "evt-1"}
    )
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["payload"] == {"note": "manual"}
