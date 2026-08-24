"""AGENTS.md settings API views."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.paths import (
    CODEX_AGENTS_MD_PATH,
    CODEX_DIRECT_LIVEKIT_INSTRUCTIONS_PATH,
    CODEX_DISPATCHER_INSTRUCTIONS_PATH,
    CODEX_HOME_DIR,
    CODEX_SUPER_AGENT_INSTRUCTIONS_PATH,
    OPENBASE_AGENTS_MD_PATH,
    OPENBASE_INSTRUCTIONS_DIR,
)
from openbase_coder_cli.thread_sync.session_manager import (
    resolve_super_agent_instructions_path,
)

logger = logging.getLogger(__name__)


class AgentsMdSerializer(serializers.Serializer):
    content = serializers.CharField(allow_blank=True, trim_whitespace=False)
    target = serializers.ChoiceField(
        choices=[
            "openbase",
            "normal",
            "super_agent",
            "direct_livekit",
            "dispatcher",
        ],
        default="openbase",
        required=False,
    )


@api_view(["GET", "PUT"])
def agents_md(request):
    """Read or write agent instruction files."""
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
        "openbase": {
            "id": "openbase",
            "label": "Openbase base instructions",
            "description": "Affects every Openbase Coder session on both backends; delivered per session, never written into the shared agent homes.",
            "path": OPENBASE_AGENTS_MD_PATH,
            "codex_home": OPENBASE_INSTRUCTIONS_DIR,
        },
        "normal": {
            "id": "normal",
            "label": "Codex home AGENTS.md",
            "description": "Your ~/.codex/AGENTS.md — applies to every Codex session in the shared home, including Openbase sessions.",
            "path": CODEX_AGENTS_MD_PATH,
            "codex_home": CODEX_HOME_DIR,
        },
        "direct_livekit": {
            "id": "direct_livekit",
            "label": "Direct voice session instructions",
            "description": "Affects agent threads that are directly connected to a LiveKit voice session after a voice transfer.",
            "path": direct_livekit_path,
            "codex_home": OPENBASE_INSTRUCTIONS_DIR,
        },
        "super_agent": {
            "id": "super_agent",
            "label": "Super Agent instructions",
            "description": "Affects normal non-dispatch Super Agent threads started or resumed by Openbase Coder.",
            "path": super_agent_path,
            "codex_home": OPENBASE_INSTRUCTIONS_DIR,
        },
        "dispatcher": {
            "id": "dispatcher",
            "label": "Dispatcher-only instructions",
            "description": "Affects only the LiveKit dispatcher that routes voice sessions and coordinates transfers.",
            "path": dispatcher_path,
            "codex_home": OPENBASE_INSTRUCTIONS_DIR,
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

    openbase_document = documents[0]
    return Response(
        {
            "content": openbase_document["content"],
            "path": openbase_document["path"],
            "codex_home": openbase_document["codex_home"],
            "documents": documents,
        },
        status=status.HTTP_200_OK,
    )
