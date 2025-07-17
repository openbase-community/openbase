import json
import logging

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from openbase.openbase_app.serializers import (
    ClaudeCodeMessageSerializer,
    ClaudeCodeResponseSerializer,
)

logger = logging.getLogger(__name__)

try:
    from claude_code_sdk import ClaudeCodeOptions, query

    CLAUDE_CODE_AVAILABLE = True
except ImportError:
    logger.warning(
        "claude_code_sdk not available. Install with: pip install claude-code-sdk"
    )
    CLAUDE_CODE_AVAILABLE = False


class ClaudeCodeViewSet(viewsets.ViewSet):
    """ViewSet for Claude Code AI interactions."""

    @action(detail=False, methods=['post'])
    def message(self, request):
        """
        Send a message to Claude Code and return the response.

        Expected JSON payload:
        {
            "message": "Your message to Claude Code",
            "system_prompt": "Optional system prompt",
            "max_turns": 1
        }
        """
        if not CLAUDE_CODE_AVAILABLE:
            raise ValidationError(
                "Claude Code SDK not available. Please install claude-code-sdk package."
            )

        serializer = ClaudeCodeMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        validated_data = serializer.validated_data
        message = validated_data["message"]
        system_prompt = validated_data["system_prompt"]
        max_turns = validated_data["max_turns"]

        # Set up Claude Code options
        options = ClaudeCodeOptions(system_prompt=system_prompt, max_turns=max_turns)

        try:
            # Call Claude Code
            response = query(message, options)
            
            # Format response based on the actual response structure
            if isinstance(response, str):
                response_data = {"response": response}
            else:
                response_data = {
                    "response": response.get("message", str(response)),
                    "conversation_id": response.get("conversation_id"),
                    "turn_count": response.get("turn_count", 1)
                }
            
            serializer = ClaudeCodeResponseSerializer(response_data)
            return Response(serializer.data)
        except Exception as e:
            logger.error(f"Error calling Claude Code: {str(e)}")
            raise ValidationError(f"Error calling Claude Code: {str(e)}")


# Keep the original function for backward compatibility if needed
async def send_message_to_claude_code(request):
    """Legacy function - deprecated, use ClaudeCodeViewSet instead."""
    viewset = ClaudeCodeViewSet()
    return viewset.message(request)
