"""AGENTS.md settings API views."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.thread_sync.session_manager import resolve_super_agent_instructions_path
from openbase_coder_cli.paths import (
    CODEX_AGENTS_MD_PATH,
    CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH,
    CODEX_DISPATCHER_INSTRUCTIONS_PATH,
    CODEX_HOME_DIR,
    CODEX_SUPER_AGENT_INSTRUCTIONS_PATH,
    NORMAL_CODEX_AGENTS_MD_PATH,
    NORMAL_CODEX_HOME_DIR,
    OPENBASE_CLAUDE_CONFIG_DIR,
    OPENBASE_CLAUDE_MD_PATH,
)

logger = logging.getLogger(__name__)


class AgentsMdSerializer(serializers.Serializer):
    content = serializers.CharField(allow_blank=True, trim_whitespace=False)
    target = serializers.ChoiceField(
        choices=[
            "voice",
            "normal",
            "claude",
            "super_agent",
            "direct_livekit",
            "dispatcher",
        ],
        default="voice",
        required=False,
    )


@api_view(["GET", "PUT"])
def agents_md(request):
    """Read or write Codex home AGENTS.md files."""
    direct_livekit_path = Path(
        os.environ.get(
            "LIVEKIT_DIRECT_CODEX_DEVELOPER_INSTRUCTIONS_PATH",
            str(CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH),
        )
    ).expanduser()
    dispatcher_path = CODEX_DISPATCHER_INSTRUCTIONS_PATH
    super_agent_path = Path(
        resolve_super_agent_instructions_path(
            default_path=CODEX_SUPER_AGENT_INSTRUCTIONS_PATH
        )
    )
    agents_targets = {
        "voice": {
            "id": "voice",
            "label": "Voice Codex home AGENTS.md",
            "description": "Affects the Openbase Coder voice Codex home environment and its general voice-coding behavior.",
            "path": CODEX_AGENTS_MD_PATH,
            "codex_home": CODEX_HOME_DIR,
        },
        "claude": {
            "id": "claude",
            "label": "Openbase Claude config CLAUDE.md",
            "description": "Affects Claude Code sessions that use Openbase's managed CLAUDE_CONFIG_DIR.",
            "path": OPENBASE_CLAUDE_MD_PATH,
            "codex_home": OPENBASE_CLAUDE_CONFIG_DIR,
        },
        "normal": {
            "id": "normal",
            "label": "Normal Codex home AGENTS.md",
            "description": "Affects regular non-voice Codex sessions that use the standard Codex home directory.",
            "path": NORMAL_CODEX_AGENTS_MD_PATH,
            "codex_home": NORMAL_CODEX_HOME_DIR,
        },
        "direct_livekit": {
            "id": "direct_livekit",
            "label": "Direct voice session instructions",
            "description": "Affects agent threads that are directly connected to a LiveKit voice session after a voice transfer.",
            "path": direct_livekit_path,
            "codex_home": CODEX_HOME_DIR,
        },
        "super_agent": {
            "id": "super_agent",
            "label": "Super Agent instructions",
            "description": "Affects normal non-dispatch Super Agent threads started or resumed by Openbase Coder.",
            "path": super_agent_path,
            "codex_home": CODEX_HOME_DIR,
        },
        "dispatcher": {
            "id": "dispatcher",
            "label": "Dispatcher-only instructions",
            "description": "Affects only the LiveKit dispatcher that routes voice sessions and coordinates transfers.",
            "path": dispatcher_path,
            "codex_home": CODEX_HOME_DIR,
        },
    }

    if request.method == "PUT":
        input_serializer = AgentsMdSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        target = agents_targets[input_serializer.validated_data["target"]]
        agents_path = target["path"]
        try:
            agents_path.parent.mkdir(parents=True, exist_ok=True)
            agents_path.write_text(
                input_serializer.validated_data["content"],
                encoding="utf-8",
            )
        except OSError as exc:
            logger.exception("Unable to write AGENTS.md")
            return Response(
                {
                    "error": f"Unable to write AGENTS.md: {exc}",
                    "path": str(agents_path),
                    "target": target["id"],
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            {
                "id": target["id"],
                "label": target["label"],
                "content": input_serializer.validated_data["content"],
                "path": str(agents_path),
                "codex_home": str(target["codex_home"]),
                "exists": True,
            },
            status=status.HTTP_200_OK,
        )

    documents = []
    errors = []
    for target in agents_targets.values():
        agents_path = target["path"]
        try:
            exists = agents_path.exists()
            content = agents_path.read_text(encoding="utf-8") if exists else ""
        except OSError as exc:
            logger.exception("Unable to read AGENTS.md")
            errors.append(
                {
                    "target": target["id"],
                    "error": f"Unable to read AGENTS.md: {exc}",
                    "path": str(agents_path),
                }
            )
            continue
        documents.append(
            {
                "id": target["id"],
                "label": target["label"],
                "description": target["description"],
                "content": content,
                "path": str(agents_path),
                "codex_home": str(target["codex_home"]),
                "exists": exists,
            }
        )

    if errors:
        first_error = errors[0]
        return Response(
            {
                "error": first_error["error"],
                "path": first_error["path"],
                "target": first_error["target"],
                "documents": documents,
                "errors": errors,
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    voice_document = documents[0]
    return Response(
        {
            "content": voice_document["content"],
            "path": voice_document["path"],
            "codex_home": voice_document["codex_home"],
            "documents": documents,
        },
        status=status.HTTP_200_OK,
    )
