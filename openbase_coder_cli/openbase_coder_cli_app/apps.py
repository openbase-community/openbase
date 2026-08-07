"""
Django app configuration for openbase_coder_cli_app.
"""

from __future__ import annotations

import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class OpenbaseCoderCliAppConfig(AppConfig):
    """Configuration for the openbase_coder_cli_app Django application."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "openbase_coder_cli.openbase_coder_cli_app"
    verbose_name = "Openbase Coder Cli"

    def ready(self):
        from openbase_coder_cli import skills_autolink
        from openbase_coder_cli.cli.setup.codex import (
            relink_workspace_skills_from_installation,
        )

        try:
            skills_autolink.sync_auto_linked_skills()
        except OSError:
            logger.exception("Unable to auto-link personal skills on startup.")
        # Cross-machine file sync can replace the agent homes' bundled skill
        # links with another machine's home paths; relink so this machine
        # self-heals on every service start.
        relink_workspace_skills_from_installation()
