import os
import subprocess

from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from openbase.config import settings

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

    @action(detail=False, methods=["post"], url_path="send-to-claude")
    def send_to_claude(self, request):
        """Send a message to Claude Code CLI and get response"""
        serializer = MessageCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # Create the user message
        message = serializer.save()

        session = message.session
        session_id = str(session.public_id)

        # Check if this is the first message for this session
        previous_messages = list(
            Message.objects.filter(session=session).order_by("created_at")
        )
        is_first_message = (
            len(previous_messages) == 1
        )  # Only the message we just created

        try:
            # Prepare Claude Code CLI command
            claude_path = os.path.expanduser("~/.claude/local/claude")
            if is_first_message:
                # New conversation
                cmd = [
                    claude_path,
                    "-p",
                    "--dangerously-skip-permissions",
                    f"--session-id={session_id}",
                    message.content,
                ]
            else:
                # Resume existing conversation
                cmd = [
                    claude_path,
                    "-p",
                    "--dangerously-skip-permissions",
                    f"--resume={session_id}",
                    message.content,
                ]

            # Execute Claude Code CLI
            print(cmd)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=settings.OPENBASE_PROJECT_PATH,
                timeout=300,  # 5 minute timeout
            )

            # Create assistant response message
            response_content = result.stdout if result.stdout else ""
            if result.stderr:
                response_content += f"\n[stderr]: {result.stderr}"

            assistant_message = Message.objects.create(
                session=session,
                content=response_content,
                role="assistant",
                claude_response={
                    "return_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
            )

            # Serialize the response
            response_serializer = MessageSerializer(assistant_message)

            return Response(
                {
                    "message": "Message sent to Claude Code successfully",
                    "user_message": MessageSerializer(message).data,
                    "assistant_response": response_serializer.data,
                }
            )

        except subprocess.TimeoutExpired:
            # Update user message with timeout error
            message.metadata = {
                **message.metadata,
                "error": "Claude Code CLI timed out after 5 minutes",
            }
            message.save()

            return Response(
                {"error": "Claude Code CLI timed out after 5 minutes"},
                status=status.HTTP_408_REQUEST_TIMEOUT,
            )

        except Exception as e:
            # Update user message with error
            message.metadata = {**message.metadata, "error": str(e)}
            message.save()

            return Response(
                {"error": f"Failed to communicate with Claude Code: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class GitDiffView(APIView):
    """Get git diff from main repo and all git subrepositories (depth 1)"""

    def _get_repo_diff(self, repo_path, repo_name):
        """Get diff for a single git repository"""
        try:
            # Get diff of tracked files (modified and staged)
            tracked_diff = subprocess.run(
                ["git", "diff", "-U1", "HEAD"],
                capture_output=True,
                text=True,
                cwd=repo_path,
                timeout=30,
            )

            # Get list of untracked files
            untracked_files = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
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
                            capture_output=True,
                            text=True,
                            cwd=repo_path,
                            timeout=30,
                        )
                        # git diff --no-index returns exit code 1 when files differ, which is expected
                        if untracked_diff.returncode in [0, 1]:
                            combined_diff += untracked_diff.stdout

            return {
                "repository": repo_name,
                "path": str(repo_path),
                "diff": combined_diff,
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
                if os.path.isdir(item_path):
                    git_dir = os.path.join(item_path, ".git")
                    if os.path.exists(git_dir):
                        # This is a git repository
                        repo_diff = self._get_repo_diff(item_path, item)
                        diffs.append(repo_diff)

            return Response(
                {
                    "repositories": diffs,
                }
            )

        except subprocess.TimeoutExpired:
            raise ValidationError("Git diff command timed out")
        except Exception as e:
            raise ValidationError(f"Failed to get git diff: {str(e)}")


class GitRecentCommitsView(APIView):
    """Get recent commits parsed into structured format"""

    def get(self, request):
        try:
            # Get the last 10 commits with detailed format for parsing
            result = subprocess.run(
                ["git", "log", "--format=%H|%h|%an|%ae|%ad|%s", "--date=iso", "-10"],
                capture_output=True,
                text=True,
                cwd=settings.OPENBASE_PROJECT_PATH,
                timeout=30,
            )

            if result.returncode != 0:
                raise ValidationError(f"Git log failed: {result.stderr}")

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
            raise ValidationError("Git log command timed out")
        except Exception as e:
            raise ValidationError(f"Failed to get recent commits: {str(e)}")
