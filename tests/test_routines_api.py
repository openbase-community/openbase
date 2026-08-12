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
