"""Conversation item ordering for backend turn snapshots."""

from typing import Any


def turn_messages(turn: dict[str, Any]) -> list[dict[str, str]]:
    """Expose item order instead of grouping all input ahead of all output."""
    from openbase_coder_cli.backend_auth import login_failure_message
    from openbase_coder_cli.cloud_model_errors import normalize_model_proxy_error

    items = turn.get("items", [])
    has_final = any(
        item.get("type") == "agentMessage"
        and isinstance(item.get("phase"), str)
        and item["phase"].startswith("final")
        and item.get("text", "").strip()
        for item in items
    )
    messages = []
    for index, item in enumerate(items):
        if item.get("type") == "userMessage":
            text = "\n\n".join(
                content.get("text", "").strip()
                for content in item.get("content", [])
                if content.get("type") == "text"
            )
            role = "user"
        elif item.get("type") == "agentMessage":
            if has_final and not str(item.get("phase", "")).startswith("final"):
                continue
            text = normalize_model_proxy_error(item.get("text", "").strip())
            role = "assistant"
        else:
            continue
        if text:
            messages.append(
                {"id": str(item.get("id") or index), "role": role, "text": text}
            )
    if login_message := login_failure_message(turn):
        # A turn that failed on a signed-out CLI: its last reply is the CLI's
        # own sign-in notice (Claude), or there is none (Codex).
        replies = [message for message in messages if message["role"] == "assistant"]
        if replies:
            replies[-1]["text"] = login_message
        elif messages:
            messages.append(
                {"id": "backend-login", "role": "assistant", "text": login_message}
            )
    # Older backend views carry output items without their input boundaries.
    # Keep the established fallback until the complete ordering is available.
    return messages if any(message["role"] == "user" for message in messages) else []
