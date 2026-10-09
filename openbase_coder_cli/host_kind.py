"""Which kind of computer this Openbase install runs on, for agent instructions.

A cloud workspace is a Linux computer in the cloud with no screen, no
Desktop folder and no desktop apps of its own; the user's phone or PC is a separate
device. An agent that is not told this answers "what's on my desktop" as if
it were sitting at the user's Mac (staging demo, 2026-10-08), or the voice
model invents an answer. The user's own computer has their files, Desktop,
apps and screen.
"""

from __future__ import annotations

import sys

HOST_KIND_CLOUD_WORKSPACE = "cloud_workspace"
HOST_KIND_MAC = "mac"
HOST_KIND_LINUX = "linux"
HOST_KIND_WINDOWS = "windows"

HOST_KIND_HEADING = "## Where this agent runs"

_HOST_NOTES = {
    HOST_KIND_CLOUD_WORKSPACE: (
        "This agent runs on the user's Openbase Cloud workspace: a Linux "
        "computer in the cloud with no screen, no Desktop folder and no desktop apps "
        "of its own. What it can see is this workspace's home directory and "
        "project folders. The user's phone and personal computer are separate "
        "devices; local filesystem commands do not inspect those devices. "
        "When the user asks about their desktop, their screen or an app on "
        "their computer, check whether available laptop tools can reach that "
        "device, and use them when appropriate. If access is unavailable, "
        "say plainly that this is their cloud workspace, offer its folders "
        "and projects, and suggest connecting to Openbase on their personal "
        "computer. Never infer that another device's desktop is empty from "
        "a workspace listing."
    ),
    HOST_KIND_MAC: (
        "This agent runs on the user's own Mac. It can see the user's files, "
        "Desktop, Documents, projects and apps, and their screen when macOS "
        "permissions allow."
    ),
    HOST_KIND_LINUX: (
        "This agent runs on the user's own Linux computer. It can see the "
        "user's files, home directory and projects."
    ),
    HOST_KIND_WINDOWS: (
        "This agent runs on the user's own Windows computer. It can see the "
        "user's files, Desktop, Documents and projects."
    ),
}

# The voice model never answers for the agent; one line keeps its
# acknowledgements honest ("checking your cloud workspace", not "your desktop").
_LIVE_VOICE_NOTES = {
    HOST_KIND_CLOUD_WORKSPACE: (
        "The caller's agent runs on their Openbase Cloud workspace, a Linux "
        "computer in the cloud with no screen or Desktop folder; the caller's "
        "phone and personal computer are separate devices. Refer to this host "
        "as their cloud workspace, never as their desktop or their Mac. "
        "Let the agent determine access to other devices and relay its answer."
    ),
    HOST_KIND_MAC: "The caller's agent runs on their own Mac.",
    HOST_KIND_LINUX: "The caller's agent runs on their own Linux computer.",
    HOST_KIND_WINDOWS: "The caller's agent runs on their own Windows computer.",
}


def host_kind() -> str:
    """The kind of computer this install runs on."""
    from openbase_coder_cli.services.cloud_workspace import cloud_workspace_id

    if cloud_workspace_id():
        return HOST_KIND_CLOUD_WORKSPACE
    if sys.platform == "darwin":
        return HOST_KIND_MAC
    if sys.platform.startswith("win"):
        return HOST_KIND_WINDOWS
    return HOST_KIND_LINUX


def host_note(kind: str | None = None) -> str:
    """One paragraph for an agent's developer instructions."""
    return _HOST_NOTES.get(kind or host_kind(), _HOST_NOTES[HOST_KIND_LINUX])


def host_section(kind: str | None = None) -> str:
    return f"{HOST_KIND_HEADING}\n\n{host_note(kind)}"


def with_host_section(instructions: str, kind: str | None = None) -> str:
    if HOST_KIND_HEADING in instructions:
        return instructions
    return f"{instructions}\n\n{host_section(kind)}"


def live_voice_host_note(kind: str | None = None) -> str:
    """One sentence for the GPT-Live persona."""
    return _LIVE_VOICE_NOTES.get(
        kind or host_kind(), _LIVE_VOICE_NOTES[HOST_KIND_LINUX]
    )
