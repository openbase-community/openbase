import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)

try:
    from claude_code_sdk import ClaudeCodeOptions, query

    CLAUDE_CODE_AVAILABLE = True
except ImportError:
    logger.warning(
        "claude_code_sdk not available. Install with: pip install claude-code-sdk"
    )
    CLAUDE_CODE_AVAILABLE = False


@api_view(["POST"])
@csrf_exempt
async def send_message_to_claude_code(request):
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

    # Parse request data
    data = json.loads(request.body)
    message = data.get("message", "")
    system_prompt = data.get("system_prompt", "You are a helpful assistant.")
    max_turns = data.get("max_turns", 1)

    if not message:
        raise ValidationError("Message is required")

    # Set up Claude Code options
    options = ClaudeCodeOptions(system_prompt=system_prompt, max_turns=max_turns)

    # Collect responses from Claude Code
    responses = []
    costs = []

    async for response_message in query(prompt=message, options=options):
        # Handle different message types defensively
        message_type = type(response_message).__name__

        if message_type == "AssistantMessage":
            # Extract text content from the assistant message
            text_content = []
            if hasattr(response_message, "content") and response_message.content:
                for block in response_message.content:
                    if hasattr(block, "text"):
                        text_content.append(str(block.text))
                    elif hasattr(block, "type") and block.type == "text":
                        text_content.append(str(block))

            if not text_content:
                text_content = [str(response_message)]

            responses.append(
                {
                    "type": "assistant",
                    "content": "\n".join(text_content),
                    "message_id": getattr(response_message, "id", None),
                }
            )

        elif message_type == "ResultMessage":
            # Handle cost/result information
            costs.append(
                {
                    "type": "result",
                    "content": str(response_message),
                    "cost_info": getattr(response_message, "cost", None),
                }
            )
        else:
            # Handle other message types
            responses.append(
                {
                    "type": "other",
                    "content": str(response_message),
                    "message_type": message_type,
                }
            )

    return JsonResponse(
        {
            "success": True,
            "responses": responses,
            "costs": costs,
            "message_count": len(responses),
        }
    )
