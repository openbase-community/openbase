"""Base class for generation commands in Openbase CLI."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from openbase.core.claude_code_helper import ClaudeCodeHelper
from openbase.core.paths import ProjectPaths, get_config_file_path
from openbase.core.project_config import ProjectConfig

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class GenerationCommand(ABC):
    """Base class for all generation commands."""

    def __init__(self, root_dir: Path):
        """Initialize the generation command with root directory."""
        self.root_dir = root_dir
        self.config = ProjectConfig.from_file(get_config_file_path(root_dir))
        self.paths = ProjectPaths(root_dir, self.config)
        self.claude_helper = ClaudeCodeHelper(project_path=root_dir)

    @abstractmethod
    async def generate_async(self) -> None:
        """Abstract method to be implemented by subclasses for async generation."""

    @abstractmethod
    def get_command_description(self) -> str:
        """Return the name of this generation command."""

    async def execute_claude_command(self, prompt: str) -> tuple[str, str, int]:
        """Execute a Claude command with the given prompt."""
        logger.info(
            f"Sending request to Claude Code to {self.get_command_description()}..."
        )
        (
            stdout,
            stderr,
            return_code,
        ) = await self.claude_helper.execute_claude_command_sync(prompt)

        if return_code != 0:
            logger.error(f"Claude Code returned non-zero exit code: {return_code}")
            if stderr:
                logger.error(f"Error output: {stderr}")
            msg = f"Failed to {self.get_command_description()}. Check the logs for details."
            raise ValueError(msg)

        logger.info(f"{self.get_command_description()} completed successfully")

        # Log the output for debugging
        if stdout:
            logger.debug(f"Claude Code output:\n{stdout}")

        return stdout, stderr, return_code

    def generate(self) -> None:
        """Synchronous wrapper for generate_async."""
        asyncio.run(self.generate_async())
