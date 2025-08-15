"""Path definitions for Openbase."""

from pathlib import Path


def get_openbase_dir() -> Path:
    """Get the Openbase home directory."""
    return Path.home() / ".openbase"


def get_boilerplate_dir() -> Path:
    """Get the boilerplate directory for templates."""
    return get_openbase_dir() / "boilerplate"