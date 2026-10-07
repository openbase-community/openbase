"""Observe developer setup's system changes without displaying config contents."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import click

from openbase_coder_cli.paths import (
    INSTALLATION_JSON_PATH,
    LAUNCHD_DOMAIN,
    OPENBASE_BIN_DIR,
    OPENBASE_DISPATCHER_CONFIG_PATH,
    PLIST_DIR,
    SYSTEMD_UNIT_DIR,
    TASK_SCHEDULER_DIR,
)
from openbase_coder_cli.services.netmesh_companion import (
    HELPER_LAUNCHD_LABEL,
    helper_launchd_health,
)


def _display_path(path: Path) -> str:
    return str(path).replace(str(Path.home()) + os.sep, "~/", 1)


def _fingerprint(path: Path, *, metadata_only: bool = False) -> str:
    link = f"symlink:{os.readlink(path)}:" if path.is_symlink() else ""
    if link and not path.exists():
        return link
    if metadata_only:
        stat = path.stat()
        return f"{link}{stat.st_size}:{stat.st_mtime_ns}:{stat.st_mode}"
    return link + hashlib.sha256(path.read_bytes()).hexdigest()


def _git_ignore_path() -> Path:
    result = subprocess.run(
        ["git", "config", "--global", "--path", "--get", "core.excludesFile"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode not in (0, 1):
        raise OSError("could not read global Git ignore configuration")
    if result.stdout.strip():
        return Path(result.stdout.strip()).expanduser()
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "git/ignore"
    )


@dataclass
class SystemSetupSnapshot:
    files: dict[str, str] = field(default_factory=dict)
    services: dict[str, str] = field(default_factory=dict)
    binaries: dict[str, tuple[int, int]] = field(default_factory=dict)
    ignores: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)

    @classmethod
    def capture(cls, env_file: Path) -> SystemSetupSnapshot:
        snapshot = cls()
        for path in (
            env_file.expanduser(),
            INSTALLATION_JSON_PATH,
            OPENBASE_DISPATCHER_CONFIG_PATH,
        ):
            snapshot._capture_file(path, snapshot.files)
        # Native agent executables can be large; only inspect their metadata.
        for name in ("openbase-coder", "claude", "codex"):
            snapshot._capture_file(
                Path.home() / ".local/bin" / name, snapshot.files, metadata_only=True
            )
        for directory, suffix in (
            (PLIST_DIR, "plist"),
            (SYSTEMD_UNIT_DIR, "service"),
            (TASK_SCHEDULER_DIR, "xml"),
        ):
            for path in directory.glob(f"{LAUNCHD_DOMAIN}.*.{suffix}"):
                snapshot._capture_file(path, snapshot.services, label=path.stem)
        helper_health = helper_launchd_health()
        if helper_health.registered:
            snapshot.services[HELPER_LAUNCHD_LABEL] = "registered VPN helper"
        elif helper_health.detail.startswith("launchctl unavailable"):
            snapshot.unreadable.append(HELPER_LAUNCHD_LABEL)
        for path in OPENBASE_BIN_DIR.glob("*"):
            try:
                if path.is_file():
                    stat = path.stat()
                    snapshot.binaries[path.name] = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                snapshot.unreadable.append(path.name)
        for label, resolve in (("Global Git", _git_ignore_path),):
            try:
                path = resolve()
                lines = path.read_text().splitlines() if path.is_file() else []
                patterns = tuple(
                    line for line in lines if line and not line.startswith("#")
                )
                snapshot.ignores[label] = (_display_path(path), patterns)
            except (OSError, subprocess.TimeoutExpired):
                snapshot.unreadable.append(f"{label} ignore rules")
        return snapshot

    def _capture_file(
        self,
        path: Path,
        target: dict[str, str],
        *,
        label: str = "",
        metadata_only: bool = False,
    ) -> None:
        try:
            if path.exists() or path.is_symlink():
                target[label or _display_path(path)] = _fingerprint(
                    path, metadata_only=metadata_only
                )
        except OSError:
            self.unreadable.append(label or _display_path(path))


def _describe_changes(
    before: dict,
    after: dict,
    noun: str,
    *,
    unknown: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    before = {key: value for key, value in before.items() if key not in unknown}
    after = {key: value for key, value in after.items() if key not in unknown}
    sentences = []
    for verb, names in (
        ("Added", after.keys() - before.keys()),
        (
            "Updated",
            {key for key in before.keys() & after.keys() if before[key] != after[key]},
        ),
        ("Removed", before.keys() - after.keys()),
    ):
        if names:
            sentences.append(f"{verb} {noun}: {', '.join(sorted(names))}.")
    return sentences


def print_system_setup_summary(
    before: SystemSetupSnapshot,
    after: SystemSetupSnapshot,
    *,
    service_manager: str,
    skip_services: bool,
    serve_healthy: bool,
) -> None:
    unknown = set(before.unreadable + after.unreadable)
    sentences = _describe_changes(
        before.services,
        after.services,
        f"{service_manager} service definitions",
        unknown=unknown,
    )
    if skip_services:
        sentences.append(
            "Background service installation was skipped (--skip-services)."
        )
    elif not sentences:
        sentences.append(f"Existing {service_manager} services were refreshed.")
    sentences += _describe_changes(
        before.files, after.files, "local configuration/launcher files", unknown=unknown
    )
    sentences += _describe_changes(
        before.binaries, after.binaries, "Openbase binaries", unknown=unknown
    )
    for label in sorted(before.ignores.keys() & after.ignores.keys()):
        old_path, old_patterns = before.ignores[label]
        new_path, new_patterns = after.ignores[label]
        if old_path != new_path:
            sentences.append(
                f"{label} ignore file changed from {old_path} to {new_path}."
            )
        changes = _describe_changes(
            dict.fromkeys(old_patterns),
            dict.fromkeys(new_patterns),
            f"{label} ignore entries in {new_path}",
        )
        if not changes and old_patterns != new_patterns:
            changes = [f"Updated {label} ignore rule order in {new_path}."]
        sentences += changes or [f"{label} ignore entries are unchanged."]
    sentences.append(
        "Private-network routes for the local API (:18080) and LiveKit (:7880) are healthy."
        if serve_healthy
        else "Private-network health was not confirmed; see the setup results above."
    )
    if before.unreadable or after.unreadable:
        sentences.append(
            "Could not compare: "
            + ", ".join(sorted(set(before.unreadable + after.unreadable)))
            + "."
        )
    click.echo()
    click.echo("ℹ️ System changes: " + " ".join(sentences))
