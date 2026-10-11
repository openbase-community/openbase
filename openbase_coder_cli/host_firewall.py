"""Whether this computer's firewall lets the phone reach the login relay.

On a desktop or VM backend the login callback relay listens on this host's
own VPN address. The macOS application firewall (System Settings > Network >
Firewall) filters incoming connections per program, so when it is on and set
to block this Python, the phone's connection is dropped and a forwarded login
never completes. A program that is merely unsigned is blocked silently once
the user dismissed (or never saw) the "accept incoming connections?" prompt,
which is typical for a developer interpreter. Reading the setting needs no
administrator rights; changing it does, so this module only reports.
"""

from __future__ import annotations

import os
import subprocess
import sys

SOCKETFILTERFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"


def _query(*args: str) -> str:
    result = subprocess.run(
        [SOCKETFILTERFW, *args], capture_output=True, text=True, timeout=5
    )
    return f"{result.stdout}\n{result.stderr}"


def blocked_interpreter(executable: str | None = None) -> str | None:
    """The Python path the macOS firewall keeps from accepting connections.

    ``None`` when nothing blocks it: another platform, the firewall off, the
    program allowed, or the setting could not be read.
    """
    if sys.platform != "darwin" or not os.path.exists(SOCKETFILTERFW):
        return None
    program = os.path.realpath(executable or sys.executable)
    try:
        # "Firewall is enabled. (State = 1)"; 0 means off.
        if "State = 0" in _query("--getglobalstate"):
            return None
        # "Firewall has block all state set to enabled." blocks every program.
        if "set to enabled" in _query("--getblockall"):
            return program
        # "Incoming connection to <path> is blocked." / "... is permitted."
        if "is blocked" in _query("--getappblocked", program):
            return program
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def unblock_hint(program: str) -> str:
    """One line telling the user how to let the relay accept the phone."""
    return (
        "macOS Firewall blocks incoming connections for this computer's Openbase "
        "Python, so your phone cannot deliver the sign-in to it. Fix it once: "
        "System Settings > Network > Firewall > Options, set "
        f"{os.path.basename(program)} to Allow, or run: sudo {SOCKETFILTERFW} "
        f"--unblockapp {program}"
    )
