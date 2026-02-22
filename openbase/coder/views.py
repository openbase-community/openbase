from __future__ import annotations

import json
import os
import pathlib
import subprocess

from asgiref.sync import sync_to_async
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from openbase.config import settings
from openbase.core.claude_code_helper import ClaudeCodeHelper

from .models import ChatSession, Message
from .serializers import (
    ChatSessionSerializer,
    MessageCreateSerializer,
    MessageSerializer,
)


class ChatSessionViewSet(viewsets.ModelViewSet):
    queryset = ChatSession.objects.all()
    serializer_class = ChatSessionSerializer
    lookup_field = "public_id"

    def get_object(self):
        return get_object_or_404(ChatSession, public_id=self.kwargs["public_id"])


class MessageViewSet(viewsets.ModelViewSet):
    queryset = Message.objects.all()
    serializer_class = MessageSerializer
    lookup_field = "public_id"

    def get_object(self):
        return get_object_or_404(Message, public_id=self.kwargs["public_id"])

    def get_serializer_class(self):
        if self.action == "create":
            return MessageCreateSerializer
        return MessageSerializer


class SendToClaudeView(APIView):
    """Send a message to Claude Code CLI and get streaming response."""

    async def post(self, request):
        # DRF handles authentication via DEFAULT_AUTHENTICATION_CLASSES
        # and permissions via DEFAULT_PERMISSION_CLASSES in settings.py

        data = request.data

        # Validate data using DRF serializer
        serializer = MessageCreateSerializer(data=data)
        if not serializer.is_valid():
            return Response({"errors": serializer.errors}, status=400)

        # Create the user message
        message = await sync_to_async(serializer.save)()

        session = message.session
        session_id = str(session.public_id)

        # Check if this is the first message for this session
        previous_messages = await sync_to_async(list)(
            Message.objects.filter(session=session).order_by("created_at")
        )
        is_first_message = (
            len(previous_messages) == 1
        )  # Only the message we just created

        async def stream_claude_response():
            try:
                # Send initial response with user message
                message_serializer = MessageSerializer(message)
                initial_data = {"type": "user_message", "data": message_serializer.data}
                yield f"data: {json.dumps(initial_data)}\n\n"

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
                import time

                last_keepalive = time.time()

                async for chunk in claude_helper.execute_claude_command(
                    prompt=message.content,
                    session_id=session_id,
                    resume_session=not is_first_message,
                ):
                    current_time = time.time()

                    if chunk["type"] == "response_chunk":
                        response_content += chunk["data"]
                        # Send each chunk as SSE event
                        chunk_data = {"type": "response_chunk", "data": chunk["data"]}
                        yield f"data: {json.dumps(chunk_data)}\n\n"
                        last_keepalive = current_time

                    elif chunk["type"] == "error_chunk":
                        stderr_content += chunk["data"]
                        error_data = {"type": "error_chunk", "data": chunk["data"]}
                        yield f"data: {json.dumps(error_data)}\n\n"
                        last_keepalive = current_time

                    elif chunk["type"] == "completion":
                        response_content = chunk["data"]["stdout"]
                        stderr_content = chunk["data"]["stderr"]
                        return_code = chunk["data"]["return_code"]
                        if stderr_content:
                            response_content += f"\n[stderr]: {stderr_content}"

                    elif chunk["type"] == "error":
                        raise Exception(chunk["data"]["error"])

                    # Send keep-alive if needed
                    if current_time - last_keepalive > 2:
                        keepalive_data = {
                            "type": "keepalive",
                            "data": {"timestamp": current_time},
                        }
                        yield f"data: {json.dumps(keepalive_data)}\n\n"
                        last_keepalive = current_time

                # Create assistant response message
                assistant_message = await sync_to_async(Message.objects.create)(
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
                final_data = {
                    "type": "completion",
                    "data": {
                        "message": "Message sent to Claude Code successfully",
                        "assistant_response": MessageSerializer(assistant_message).data,
                    },
                }
                yield f"data: {json.dumps(final_data)}\n\n"

            except TimeoutError:
                # Update user message with timeout error
                message.metadata = {
                    **message.metadata,
                    "error": "Claude Code CLI timed out",
                }
                await sync_to_async(message.save)()

                error_data = {
                    "type": "error",
                    "data": {"error": "Claude Code CLI timed out"},
                }
                yield f"data: {json.dumps(error_data)}\n\n"

            except Exception as e:
                # Update user message with error
                message.metadata = {**message.metadata, "error": str(e)}
                await sync_to_async(message.save)()

                error_data = {
                    "type": "error",
                    "data": {"error": f"Failed to communicate with Claude Code: {e!s}"},
                }
                yield f"data: {json.dumps(error_data)}\n\n"

        return StreamingHttpResponse(
            stream_claude_response(),
            content_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Cache-Control",
            },
        )


class GitDiffView(APIView):
    """Get git diff from main repo and all git subrepositories (depth 1)"""

    def _get_repo_diff(self, repo_path, repo_name):
        """Get diff for a single git repository"""
        try:
            # Get diff of tracked files (modified and staged)
            tracked_diff = subprocess.run(
                ["git", "diff", "-U1", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
                cwd=repo_path,
                timeout=30,
            )

            # Get list of untracked files
            untracked_files = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                check=False,
                capture_output=True,
                text=True,
                cwd=repo_path,
                timeout=30,
            )

            combined_diff = tracked_diff.stdout

            # Add diffs for untracked files
            if untracked_files.stdout:
                for file_path in untracked_files.stdout.strip().split("\n"):
                    if file_path:
                        # Get diff of untracked file against /dev/null
                        untracked_diff = subprocess.run(
                            [
                                "git",
                                "diff",
                                "--no-index",
                                "-U1",
                                "/dev/null",
                                file_path,
                            ],
                            check=False,
                            capture_output=True,
                            text=True,
                            cwd=repo_path,
                            timeout=30,
                        )
                        # git diff --no-index returns exit code 1 when files differ, which is expected
                        if untracked_diff.returncode in [0, 1]:
                            combined_diff += untracked_diff.stdout

            # Modify diff to include repo name in file paths
            if repo_name != ".":
                modified_diff = (
                    combined_diff.replace("diff --git a/", f"diff --git a/{repo_name}/")
                    .replace("diff --git b/", f"diff --git b/{repo_name}/")
                    .replace("\n--- a/", f"\n--- a/{repo_name}/")
                    .replace("\n+++ b/", f"\n+++ b/{repo_name}/")
                )
            else:
                modified_diff = combined_diff

            return {
                "repository": repo_name,
                "path": str(repo_path),
                "diff": modified_diff,
            }

        except Exception as e:
            return {
                "repository": repo_name,
                "path": str(repo_path),
                "diff": "",
                "error": str(e),
            }

    def get(self, request):
        try:
            diffs = []
            base_path = settings.OPENBASE_PROJECT_PATH

            # Get diff from main repository
            main_diff = self._get_repo_diff(base_path, ".")
            diffs.append(main_diff)

            # Find git repositories in immediate subdirectories
            for item in os.listdir(base_path):
                item_path = os.path.join(base_path, item)
                if pathlib.Path(item_path).is_dir():
                    git_dir = os.path.join(item_path, ".git")
                    if pathlib.Path(git_dir).exists():
                        # This is a git repository
                        repo_diff = self._get_repo_diff(item_path, item)
                        diffs.append(repo_diff)

            return Response(
                {
                    "repositories": diffs,
                }
            )

        except subprocess.TimeoutExpired:
            msg = "Git diff command timed out"
            raise ValidationError(msg)
        except Exception as e:
            msg = f"Failed to get git diff: {e!s}"
            raise ValidationError(msg)


class GitRecentCommitsView(APIView):
    """Get recent commits parsed into structured format"""

    def get(self, request):
        try:
            # Get the last 10 commits with detailed format for parsing
            result = subprocess.run(
                ["git", "log", "--format=%H|%h|%an|%ae|%ad|%s", "--date=iso", "-10"],
                check=False,
                capture_output=True,
                text=True,
                cwd=settings.OPENBASE_PROJECT_PATH,
                timeout=30,
            )

            if result.returncode != 0:
                msg = f"Git log failed: {result.stderr}"
                raise ValidationError(msg)

            # Parse the output into structured data
            commits = []
            for line in result.stdout.strip().split("\n"):
                if line:
                    parts = line.split("|", 5)
                    if len(parts) == 6:
                        commits.append(
                            {
                                "hash": parts[0],
                                "short_hash": parts[1],
                                "author_name": parts[2],
                                "author_email": parts[3],
                                "date": parts[4],
                                "message": parts[5],
                            }
                        )

            return Response(
                {
                    "commits": commits,
                }
            )

        except subprocess.TimeoutExpired:
            msg = "Git log command timed out"
            raise ValidationError(msg)
        except Exception as e:
            msg = f"Failed to get recent commits: {e!s}"
            raise ValidationError(msg)


class AbortClaudeCommandsView(APIView):
    """Kill all claude processes running on the machine"""

    def post(self, request):
        try:
            # Find all claude processes using pgrep
            find_result = subprocess.run(
                ["pgrep", "-f", "claude"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            if find_result.returncode != 0 or not find_result.stdout.strip():
                return Response(
                    {
                        "message": "No claude processes found to kill",
                    }
                )

            # Get the process IDs
            process_ids = find_result.stdout.strip().split("\n")
            killed_count = 0

            # Kill each process
            for pid in process_ids:
                if pid.strip():
                    try:
                        subprocess.run(
                            ["kill", "-TERM", pid.strip()],
                            timeout=5,
                            check=True,
                        )
                        killed_count += 1
                    except subprocess.CalledProcessError:
                        # Process might have already exited, continue with others
                        pass

            return Response(
                {
                    "message": "Claude processes terminated successfully",
                }
            )

        except subprocess.TimeoutExpired:
            msg = "Command timed out while trying to kill claude processes"
            raise ValidationError(msg)
        except Exception as e:
            msg = f"Failed to kill claude processes: {e!s}"
            raise ValidationError(msg)
