"""Helper module for interacting with Claude Code CLI."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


class ClaudeCodeHelper:
    """Helper class for interacting with Claude Code CLI."""

    def __init__(
        self,
        project_path: Path | str,
        mcp_config_path: Path | str | None = None,
        claude_path: Path | str | None = None,
    ):
        """Initialize the Claude Code helper.

        Args:
            project_path: The path to the project directory
            mcp_config_path: Optional path to MCP config file
            claude_path: Optional path to Claude Code CLI executable
        """
        self.project_path = (
            Path(project_path) if isinstance(project_path, str) else project_path
        )
        self.mcp_config_path = (
            Path(mcp_config_path)
            if isinstance(mcp_config_path, str)
            else mcp_config_path
        ) or Path.home() / ".openbase" / "mcp.json"
        self.claude_path = (
            Path(claude_path) if isinstance(claude_path, str) else claude_path
        ) or Path.home() / ".claude" / "local" / "claude"

    async def execute_claude_command(
        self,
        prompt: str,
        session_id: str | None = None,
        resume_session: bool = False,
    ) -> AsyncGenerator[dict]:
        """Execute a Claude Code command and yield response chunks.

        Args:
            prompt: The prompt to send to Claude Code
            session_id: Optional session ID for the conversation
            resume_session: Whether to resume an existing session

        Yields:
            dict: Response chunks with type and data
        """
        try:
            # Build command
            cmd = [
                str(self.claude_path),
                "-p",
                "--dangerously-skip-permissions",
                "--mcp-config",
                str(self.mcp_config_path),
                "--",
            ]

            if session_id:
                if resume_session:
                    cmd.append(f"--resume={session_id}")
                else:
                    cmd.append(f"--session-id={session_id}")

            cmd.append(prompt)

            logger.info(f"Executing Claude Code command: {' '.join(cmd)}")

            # Execute command
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.project_path),
            )

            response_content = ""
            stderr_content = ""

            # Stream stdout
            async for line in process.stdout:
                line_text = line.decode("utf-8")
                response_content += line_text
                yield {"type": "response_chunk", "data": line_text}

            # Wait for process completion and capture stderr
            await process.wait()
            if process.stderr:
                stderr_data = await process.stderr.read()
                stderr_content = stderr_data.decode("utf-8")
                if stderr_content:
                    response_content += f"\n[stderr]: {stderr_content}"
                    yield {
                        "type": "error_chunk",
                        "data": f"\n[stderr]: {stderr_content}",
                    }

            # Send completion event
            yield {
                "type": "completion",
                "data": {
                    "stdout": response_content.replace(
                        f"\n[stderr]: {stderr_content}", ""
                    )
                    if stderr_content
                    else response_content,
                    "stderr": stderr_content,
                    "return_code": process.returncode,
                },
            }

        except TimeoutError:
            logger.error("Claude Code CLI timed out")
            yield {"type": "error", "data": {"error": "Claude Code CLI timed out"}}

        except Exception as e:
            logger.error(f"Failed to communicate with Claude Code: {e!s}")
            yield {
                "type": "error",
                "data": {"error": f"Failed to communicate with Claude Code: {e!s}"},
            }

    async def execute_claude_command_sync(
        self,
        prompt: str,
        session_id: str | None = None,
        resume_session: bool = False,
    ) -> tuple[str, str, int]:
        """Execute a Claude Code command and return the complete result.

        Args:
            prompt: The prompt to send to Claude Code
            session_id: Optional session ID for the conversation
            resume_session: Whether to resume an existing session

        Returns:
            tuple: (stdout, stderr, return_code)
        """
        stdout = ""
        stderr = ""
        return_code = 0

        async for chunk in self.execute_claude_command(
            prompt, session_id, resume_session
        ):
            if chunk["type"] == "response_chunk":
                stdout += chunk["data"]
            elif chunk["type"] == "error_chunk":
                stderr += chunk["data"]
            elif chunk["type"] == "completion":
                stdout = chunk["data"]["stdout"]
                stderr = chunk["data"]["stderr"]
                return_code = chunk["data"]["return_code"]
            elif chunk["type"] == "error":
                stderr = chunk["data"]["error"]
                return_code = 1

        return stdout, stderr, return_code
