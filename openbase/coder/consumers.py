from __future__ import annotations

import json
import pathlib
import time

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import AnonymousUser

from openbase.config import settings
from openbase.core.claude_code_helper import ClaudeCodeHelper

from .models import Message
from .serializers import MessageCreateSerializer, MessageSerializer


class ClaudeCodeConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for communicating with Claude Code CLI"""

    async def connect(self):
        """Accept WebSocket connection if authenticated"""
        # Authentication is handled by middleware
        # Check if user is authenticated
        user = self.scope.get("user")
        if not user or isinstance(user, AnonymousUser):
            await self.close(code=4001, reason="Authentication required")
            return

        self.user = user
        await self.accept()

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection"""

    async def receive(self, text_data=None, bytes_data=None):
        """Handle incoming WebSocket messages"""
        # We only handle text data for this consumer
        if bytes_data:
            await self.send(
                text_data=json.dumps(
                    {"type": "error", "data": {"error": "Binary data not supported"}}
                )
            )
            return

        if not text_data:
            return
        try:
            data = json.loads(text_data)

            # Validate data using DRF serializer
            serializer = MessageCreateSerializer(data=data)
            if not await database_sync_to_async(serializer.is_valid)():
                await self.send(
                    text_data=json.dumps(
                        {"type": "error", "data": {"errors": serializer.errors}}
                    )
                )
                return

            # Create the user message
            message = await database_sync_to_async(serializer.save)()

            session = message.session
            session_id = str(session.public_id)

            # Check if this is the first message for this session
            previous_messages = await database_sync_to_async(list)(
                Message.objects.filter(session=session).order_by("created_at")
            )
            is_first_message = (
                len(previous_messages) == 1
            )  # Only the message we just created

            # Send initial response with user message
            message_serializer = MessageSerializer(message)
            await self.send(
                text_data=json.dumps(
                    {"type": "user_message", "data": message_serializer.data}
                )
            )

            # Stream Claude response
            await self.stream_claude_response(
                message=message,
                session=session,
                session_id=session_id,
                is_first_message=is_first_message,
            )

        except json.JSONDecodeError:
            await self.send(
                text_data=json.dumps(
                    {"type": "error", "data": {"error": "Invalid JSON"}}
                )
            )
        except Exception as e:  # noqa: BLE001
            # Catch all exceptions to ensure we send an error response to the client
            await self.send(
                text_data=json.dumps({"type": "error", "data": {"error": str(e)}})
            )

    async def stream_claude_response(
        self, message, session, session_id, is_first_message
    ):
        """Stream response from Claude Code CLI"""
        try:
            # Initialize Claude Code helper
            claude_helper = ClaudeCodeHelper(
                project_path=settings.OPENBASE_PROJECT_PATH,
                mcp_config_path=pathlib.Path("~/.openbase/mcp.json").expanduser(),
                claude_path=pathlib.Path("~/.claude/local/claude").expanduser(),
            )

            response_content = ""
            stderr_content = ""
            return_code = 0

            # Stream response with keep-alive
            last_keepalive = time.time()

            async for chunk in claude_helper.execute_claude_command(
                prompt=message.content,
                session_id=session_id,
                resume_session=not is_first_message,
            ):
                current_time = time.time()

                if chunk["type"] == "response_chunk":
                    response_content += chunk["data"]
                    # Send each chunk as WebSocket message
                    await self.send(
                        text_data=json.dumps(
                            {"type": "response_chunk", "data": chunk["data"]}
                        )
                    )
                    last_keepalive = current_time

                elif chunk["type"] == "error_chunk":
                    stderr_content += chunk["data"]
                    await self.send(
                        text_data=json.dumps(
                            {"type": "error_chunk", "data": chunk["data"]}
                        )
                    )
                    last_keepalive = current_time

                elif chunk["type"] == "completion":
                    response_content = chunk["data"]["stdout"]
                    stderr_content = chunk["data"]["stderr"]
                    return_code = chunk["data"]["return_code"]
                    if stderr_content:
                        response_content += f"\n[stderr]: {stderr_content}"

                elif chunk["type"] == "error":
                    raise RuntimeError(chunk["data"]["error"])

                # Send keep-alive if needed
                if current_time - last_keepalive > 2:
                    await self.send(
                        text_data=json.dumps(
                            {"type": "keepalive", "data": {"timestamp": current_time}}
                        )
                    )
                    last_keepalive = current_time

            # Create assistant response message
            assistant_message = await database_sync_to_async(Message.objects.create)(
                session=session,
                content=response_content,
                role="assistant",
                claude_response={
                    "return_code": return_code,
                    "stdout": response_content.replace(
                        f"\n[stderr]: {stderr_content}", ""
                    )
                    if stderr_content
                    else response_content,
                    "stderr": stderr_content,
                },
            )

            # Send final completion event
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "completion",
                        "data": {
                            "message": "Message sent to Claude Code successfully",
                            "assistant_response": MessageSerializer(
                                assistant_message
                            ).data,
                        },
                    }
                )
            )

        except TimeoutError:
            # Update user message with timeout error
            message.metadata = {
                **message.metadata,
                "error": "Claude Code CLI timed out",
            }
            await database_sync_to_async(message.save)()

            await self.send(
                text_data=json.dumps(
                    {"type": "error", "data": {"error": "Claude Code CLI timed out"}}
                )
            )

        except Exception as e:  # noqa: BLE001
            # Catch all exceptions to ensure we update the message and notify the client
            # Update user message with error
            message.metadata = {**message.metadata, "error": str(e)}
            await database_sync_to_async(message.save)()

            await self.send(
                text_data=json.dumps(
                    {
                        "type": "error",
                        "data": {
                            "error": f"Failed to communicate with Claude Code: {e!s}"
                        },
                    }
                )
            )
