"""Path definitions for Openbase."""

from pathlib import Path


def get_openbase_settings_dir() -> Path:
    """Get the Openbase home directory."""
    path = Path.home() / ".openbase"
    path.mkdir(exist_ok=True)
    return path


def get_boilerplate_dir() -> Path:
    """Get the boilerplate directory for templates."""
    path = get_openbase_settings_dir() / "boilerplate"
    return path
