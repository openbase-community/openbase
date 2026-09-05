"""Resolve the Openbase Cloud environment for the installed release channel."""

from __future__ import annotations

import os

from openbase_coder_cli.runtime import current_runtime_package

WEB_BACKEND_ENV_KEY = "OPENBASE_CODER_CLI_WEB_BACKEND_URL"
PRODUCTION_WEB_BACKEND_URL = "https://app.openbase.cloud"
STAGING_WEB_BACKEND_URL = "https://app-staging.openbase.cloud"


def default_web_backend_url() -> str:
    """Return the Cloud URL implied by the standalone package channel."""
    package = current_runtime_package()
    if package is not None and package.channel == "staging":
        return STAGING_WEB_BACKEND_URL
    return PRODUCTION_WEB_BACKEND_URL


def configured_web_backend_url() -> str:
    """Return an explicit Cloud override or the package-channel default."""
    override = os.environ.get(WEB_BACKEND_ENV_KEY) or _env_file_web_backend_url()
    return (override or default_web_backend_url()).rstrip("/")


def _env_file_web_backend_url() -> str | None:
    """The install's persisted override, read at call time.

    Managed services get the .env sourced by their launch wrappers, but bare
    CLI commands (``openbase-coder login``) do not — without this fallback a
    staging-configured install would silently target production.
    """
    from openbase_coder_cli import paths
    from openbase_coder_cli.env_file import env_file_values

    return env_file_values(paths.DEFAULT_ENV_FILE_PATH).get(WEB_BACKEND_ENV_KEY)
