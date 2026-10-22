"""Messaging sources: the local or remote `whatsapp-local` / `imessage-local` CLIs.

Each source is one skill installation. A source runs on this machine or on
the canonical host reached over SSH (the WhatsApp skill keeps one archive per
account on one always-on host). Only metadata commands are used here; the
message text stays behind the skills' own approval gates.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from openbase_coder_cli.messaging_contacts.identity import (
    Identity,
    imessage_identity,
    whatsapp_identity,
)

SOURCE_KINDS = ("whatsapp", "imessage")
KIND_LABELS = {"whatsapp": "WhatsApp", "imessage": "iMessage"}
DEFAULT_COMMANDS = {"whatsapp": "whatsapp-local", "imessage": "imessage-local"}
SSH_TARGET_ENV = {
    "whatsapp": "WHATSAPP_CLI_SSH_TARGET",
    "imessage": "IMESSAGE_CLI_SSH_TARGET",
}
# The skills' own page caps: iMessage serves at most 100 rows, WhatsApp 5000.
PAGE_SIZE = {"whatsapp": 1000, "imessage": 100}
COMMAND_TIMEOUT_SECONDS = 60
REMOTE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


class MessagingSourceError(Exception):
    """A source could not be read. The message never carries contact data."""


@dataclass(frozen=True)
class Source:
    id: str
    kind: str
    command: tuple[str, ...]
    ssh_target: str | None = None
    label: str | None = None
    merge: bool = True

    @property
    def title(self) -> str:
        return self.label or KIND_LABELS.get(self.kind, self.kind)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.title,
            "command": list(self.command),
            "ssh_target": self.ssh_target,
            "merge": self.merge,
        }


def source_from_config(payload: dict[str, Any]) -> Source:
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in SOURCE_KINDS:
        raise ValueError(f"source kind must be one of: {', '.join(SOURCE_KINDS)}")
    raw_command = payload.get("command")
    if isinstance(raw_command, str):
        command = tuple(shlex.split(raw_command))
    elif isinstance(raw_command, list) and all(
        isinstance(part, str) for part in raw_command
    ):
        command = tuple(raw_command)
    elif raw_command is None:
        command = (DEFAULT_COMMANDS[kind],)
    else:
        raise ValueError("source command must be a string or a list of strings")
    if not command:
        raise ValueError("source command is required")
    source_id = str(payload.get("id") or kind).strip()
    if not source_id:
        raise ValueError("source id is required")
    ssh_target = str(payload.get("ssh_target") or "").strip() or None
    label = str(payload.get("label") or "").strip() or None
    return Source(
        id=source_id,
        kind=kind,
        command=command,
        ssh_target=ssh_target,
        label=label,
        merge=bool(payload.get("merge", True)),
    )


def configured_sources(config: list[dict[str, Any]] | None) -> list[Source]:
    """Explicitly configured sources, or the ones detectable on this machine."""
    if config is not None:
        return [source_from_config(item) for item in config if isinstance(item, dict)]
    return detect_sources()


def detect_sources(environ: dict[str, str] | None = None) -> list[Source]:
    """Find skill CLIs on PATH or canonical hosts named by the skills' env vars."""
    env = os.environ if environ is None else environ
    sources: list[Source] = []
    for kind in SOURCE_KINDS:
        command = DEFAULT_COMMANDS[kind]
        ssh_target = str(env.get(SSH_TARGET_ENV[kind]) or "").strip() or None
        if ssh_target:
            sources.append(
                Source(id=kind, kind=kind, command=(command,), ssh_target=ssh_target)
            )
        elif shutil.which(command):
            sources.append(Source(id=kind, kind=kind, command=(command,)))
    return sources


def run_source_json(
    source: Source, args: list[str], *, timeout: float = COMMAND_TIMEOUT_SECONDS
) -> Any:
    """Run one metadata command with --json and parse its output."""
    argv = [*source.command, *args, "--json"]
    if source.ssh_target:
        remote = f"PATH={REMOTE_PATH}:$PATH {shlex.join(argv)}"
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            source.ssh_target,
            remote,
        ]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise MessagingSourceError(f"{source.title}: command not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise MessagingSourceError(
            f"{source.title}: timed out after {int(timeout)}s"
        ) from exc
    if completed.returncode != 0:
        # stderr may name a contact; report only the exit status.
        raise MessagingSourceError(
            f"{source.title}: command exited with status {completed.returncode}"
        )
    try:
        return json.loads(completed.stdout or "null")
    except json.JSONDecodeError as exc:
        raise MessagingSourceError(
            f"{source.title}: command did not return JSON"
        ) from exc


def scan_source(source: Source, runner=run_source_json) -> list[Identity]:
    """Every contact and conversation the source knows, as identities."""
    if source.kind == "whatsapp":
        rows = _paged(source, ["contacts"], runner)
        identities = (
            whatsapp_identity(row, source_id=source.id, mergeable=source.merge)
            for row in rows
        )
    else:
        rows = [
            *_paged(source, ["contacts"], runner),
            *_paged(source, ["conversations"], runner),
        ]
        identities = (
            imessage_identity(row, source_id=source.id, mergeable=source.merge)
            for row in rows
        )
    return [identity for identity in identities if identity is not None]


def _paged(source: Source, args: list[str], runner) -> list[dict[str, Any]]:
    page_size = PAGE_SIZE[source.kind]
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = runner(
            source, [*args, "--limit", str(page_size), "--offset", str(offset)]
        )
        items = _rows(page)
        rows.extend(items)
        if len(items) < page_size:
            return rows
        offset += page_size


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = (
            payload.get("contacts")
            or payload.get("rows")
            or payload.get("entities")
            or []
        )
    if not isinstance(payload, list):
        raise MessagingSourceError("command returned an unexpected JSON shape")
    return [row for row in payload if isinstance(row, dict)]
