"""``openbase-coder sync`` — managed file sync between the user's computers."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import click

from openbase_coder_cli import sync_config
from openbase_coder_cli.code_sync import CodeSyncError
from openbase_coder_cli.code_sync import manager as sync_manager
from openbase_coder_cli.code_sync.conflicts import (
    resolve_conflict,
    unresolved_conflicts,
)
from openbase_coder_cli.code_sync.eligibility import current_eligibility

DEFAULT_RECONCILE_INTERVAL_SECONDS = 60.0


@click.group()
def sync() -> None:
    """Keep code in sync between your computers (managed Syncthing)."""


@sync.command("install-engine")
def install_engine() -> None:
    """Pre-fetch the pinned Syncthing engine without enabling sync.

    Useful for baking the binary into a DevSpace AMI or warming a laptop so
    that enabling sync later is instant and needs no network.
    """
    from openbase_coder_cli.code_sync.install import ensure_syncthing_installed

    path = ensure_syncthing_installed()
    click.echo(f"Syncthing engine ready at {path}")


@sync.command()
@click.option(
    "--force",
    is_flag=True,
    help="Enable even when the cloud registry does not yet show two devices.",
)
def enable(force: bool) -> None:
    """Enable code sync: identity, config, ignores, and the sync service."""
    try:
        summary = sync_manager.enable_code_sync(force=force)
    except CodeSyncError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"Code sync enabled. Syncthing device ID: {summary['device_id']}")
    click.echo(f"Peers configured: {summary['peer_count']}")
    if not summary["registered"]:
        click.echo(
            click.style(
                "  WARN  Could not advertise sync capabilities to Openbase "
                "Cloud; run 'openbase-coder login' and re-run sync enable.",
                fg="yellow",
            )
        )
    _echo_status()


@sync.command()
def disable() -> None:
    """Disable code sync and remove the service (local data is kept)."""
    summary = sync_manager.disable_code_sync()
    click.echo(
        "Code sync disabled."
        + (" Removed code-sync service." if summary["service_removed"] else "")
    )


@sync.command()
def status() -> None:
    """Show code sync eligibility, folders, and conflicts."""
    _echo_status()


def _echo_status() -> None:
    enabled = sync_config.code_sync_enabled()
    eligibility = current_eligibility()
    click.echo()
    click.echo(f"Enabled:   {'yes' if enabled else 'no'}")
    click.echo(
        "Eligible:  "
        + ("yes" if eligibility.eligible else f"no ({eligibility.reason})")
    )
    click.echo(f"Lease:     {sync_config.lease_mode()}")
    folders = sync_config.sync_folders()
    click.echo(f"Folders:   {len(folders)}")
    for folder in folders:
        click.echo(f"  {folder.folder_id}  ~/{folder.relpath}")
    if eligibility.peers:
        click.echo("Peers:")
        for peer in eligibility.peers:
            syncthing_note = (
                peer.syncthing_device_id[:15] + "…"
                if peer.syncthing_device_id
                else "no syncthing identity yet"
            )
            click.echo(
                f"  {peer.name} ({peer.tailscale_magic_dns.rstrip('.')}) "
                f"[{syncthing_note}]"
            )
    conflict_count = len(unresolved_conflicts())
    if conflict_count:
        click.echo(
            click.style(
                f"Conflicts: {conflict_count} (see 'openbase-coder sync conflicts')",
                fg="yellow",
            )
        )
    else:
        click.echo("Conflicts: 0")


@sync.command()
@click.argument("path", type=click.Path(path_type=Path))
def add(path: Path) -> None:
    """Add a directory under $HOME to code sync."""
    try:
        relpath = sync_config.relpath_for_path(path)
        folder = sync_config.add_sync_folder(relpath)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"Added ~/{folder.relpath} (folder id {folder.folder_id}).")
    _apply_if_enabled()


@sync.command()
@click.argument("path", type=click.Path(path_type=Path))
def remove(path: Path) -> None:
    """Remove a directory from code sync (files stay on disk)."""
    try:
        relpath = sync_config.relpath_for_path(path)
        removed = sync_config.remove_sync_folder(relpath)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    if not removed:
        raise click.ClickException(f"~/{relpath} is not a synced folder.")
    click.echo(f"Removed ~/{relpath} from code sync.")
    _apply_if_enabled()


def _apply_if_enabled() -> None:
    try:
        summary = sync_manager.apply_settings_change()
    except CodeSyncError as exc:
        click.echo(click.style(f"  WARN  {exc}", fg="yellow"))
        return
    if summary.get("applied"):
        click.echo("Re-rendered Syncthing config and managed ignores.")


def _resolve_ignore_folder(folder: str | None):
    """Pick the synced folder to edit ignores for (default: the code folder)."""
    folders = sync_config.sync_folders()
    if not folders:
        raise click.ClickException("No synced folders are configured.")
    if folder:
        try:
            match = sync_config.folder_for_relpath(folder)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        if match is None:
            raise click.ClickException(f"~/{folder} is not a synced folder.")
        return match
    for candidate in folders:
        if candidate.relpath == "Projects":
            return candidate
    if len(folders) == 1:
        return folders[0]
    names = ", ".join(f"~/{f.relpath}" for f in folders)
    raise click.ClickException(
        f"Multiple synced folders ({names}); pass --folder RELPATH."
    )


@sync.group()
def ignores() -> None:
    """Manage custom Syncthing ignore rules for a synced folder.

    Rules are added on top of the managed defaults (.git, node_modules,
    lockfiles, …) and re-render the folder's .stignore.
    """


@ignores.command("list")
@click.option(
    "--folder", default=None, help="Folder relpath (default: the code folder)."
)
def ignores_list(folder: str | None) -> None:
    """Show custom ignore rules for a synced folder."""
    target = _resolve_ignore_folder(folder)
    click.echo(f"Custom ignore rules for ~/{target.relpath}:")
    if not target.extra_ignores:
        click.echo("  (none)")
        return
    for pattern in target.extra_ignores:
        click.echo(f"  {pattern}")


@ignores.command("add")
@click.argument("pattern")
@click.option(
    "--folder", default=None, help="Folder relpath (default: the code folder)."
)
def ignores_add(pattern: str, folder: str | None) -> None:
    """Add a custom ignore PATTERN (Syncthing .stignore syntax)."""
    target = _resolve_ignore_folder(folder)
    try:
        updated = sync_config.add_folder_ignore(target.relpath, pattern)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"Ignoring '{pattern.strip()}' in ~/{updated.relpath}.")
    _apply_if_enabled()


@ignores.command("remove")
@click.argument("pattern")
@click.option(
    "--folder", default=None, help="Folder relpath (default: the code folder)."
)
def ignores_remove(pattern: str, folder: str | None) -> None:
    """Remove a custom ignore PATTERN from a synced folder."""
    target = _resolve_ignore_folder(folder)
    removed = sync_config.remove_folder_ignore(target.relpath, pattern)
    if not removed:
        raise click.ClickException(
            f"'{pattern.strip()}' is not a custom ignore rule for ~/{target.relpath}."
        )
    click.echo(f"Removed '{pattern.strip()}' from ~/{target.relpath}.")
    _apply_if_enabled()


@sync.command()
def conflicts() -> None:
    """List unresolved sync conflicts."""
    records = unresolved_conflicts()
    if not records:
        click.echo("No unresolved sync conflicts.")
        return
    for record in records:
        if record.get("kind") == "branch":
            click.echo(
                f"{record['id']}  branch  {record.get('repo_relpath') or '.'}"
                f"@{record.get('branch')}  local {str(record.get('local_sha'))[:12]}"
                f" vs remote {str(record.get('remote_sha'))[:12]}"
                f"  ({record.get('detected_at')})"
            )
        else:
            click.echo(
                f"{record['id']}  file    {record.get('path')}"
                f"  ({record.get('detected_at')})"
            )


@sync.command()
@click.argument("conflict_id")
@click.option(
    "--keep-local",
    "action",
    flag_value="keep_local",
    help="Keep this machine's version.",
)
@click.option(
    "--use-remote",
    "action",
    flag_value="use_remote",
    help="Adopt the peer's version (worktree is safety-stashed first).",
)
def resolve(conflict_id: str, action: str | None) -> None:
    """Resolve one conflict by id."""
    if not action:
        raise click.ClickException("Pass --keep-local or --use-remote.")
    try:
        record = resolve_conflict(conflict_id, action)
    except CodeSyncError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"Resolved {record['id']} with {action}.")


@sync.command()
@click.option("--loop", is_flag=True, help="Run forever (service mode).")
@click.option(
    "--interval",
    default=DEFAULT_RECONCILE_INTERVAL_SECONDS,
    show_default=True,
    type=float,
)
def reconcile(loop: bool, interval: float) -> None:
    """Reconcile git branch pointers with peers (one tick or a loop)."""
    from openbase_coder_cli.code_sync.reconciler import run_tick_if_enabled

    if not loop:
        summary = run_tick_if_enabled()
        if summary is None:
            raise click.ClickException("Code sync is disabled.")
        click.echo(json.dumps(summary, indent=2, sort_keys=True))
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(asctime)s %(name)s %(message)s",
    )
    logger = logging.getLogger(__name__)
    poll_interval = max(interval, 5.0)
    logger.info("code_sync reconcile_loop_started interval=%s", poll_interval)
    while True:
        started = time.monotonic()
        try:
            summary = run_tick_if_enabled()
        except Exception:
            logger.exception("code_sync tick_failed")
        else:
            if summary is not None:
                logger.info(
                    "code_sync tick_complete repos=%s conflicts=%s",
                    len(summary.get("repos", [])),
                    summary.get("conflicts_count"),
                )
        elapsed = time.monotonic() - started
        time.sleep(max(poll_interval - elapsed, 1.0))
