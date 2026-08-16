"""Host platform detection shared by the CLI, setup, and the service layer.

The runtime supports three hosts, each with its own service supervisor:
macOS uses launchd, Linux uses systemd user units, and Windows uses the
supervisor in :mod:`openbase_coder_cli.services.windows`.
"""

from __future__ import annotations

import platform

MACOS = "Darwin"
LINUX = "Linux"
WINDOWS = "Windows"

SUPPORTED_SYSTEMS = (MACOS, LINUX, WINDOWS)


def current_system() -> str:
    return platform.system()


def is_macos() -> bool:
    return current_system() == MACOS


def is_linux() -> bool:
    return current_system() == LINUX


def is_windows() -> bool:
    return current_system() == WINDOWS


def is_supported() -> bool:
    return current_system() in SUPPORTED_SYSTEMS


def service_manager_name() -> str:
    """Name of the supervisor that owns background services on this host."""
    if is_macos():
        return "launchd"
    if is_windows():
        return "Windows"
    return "systemd"
