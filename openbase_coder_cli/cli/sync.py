"""``openbase-coder sync`` — Openbase Sync between the user's computers.

Everyday commands (status, conflicts, resolve) talk to the Openbase Sync
daemon over its control socket; ``sync-daemon`` holds the setup commands.
``migrate-from-syncthing`` moves a computer off the previous sync engine.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import click

from openbase_coder_cli import sync_daemon, sync_migration


@click.group()
def sync() -> None:
    """Keep your computers in sync with Openbase Sync."""


def _client() -> sync_daemon.SyncDaemonClient:
    return sync_daemon.SyncDaemonClient()


def _require_configured() -> None:
    if not sync_daemon.is_configured():
        raise click.ClickException(
            "Openbase Sync is not set up on this computer. Run "
            "'openbase-coder sync-daemon configure' (see 'openbase-coder "
            "sync migrate-from-syncthing' if this computer used the previous "
            "sync)."
        )


@sync.command()
@click.option("--json", "as_json", is_flag=True, help="Print the raw status.")
def status(as_json: bool) -> None:
    """Show the role, roots, peers and open conflicts of Openbase Sync."""
    _require_configured()
    try:
        payload = _client().status()
    except sync_daemon.SyncDaemonError as exc:
        raise click.ClickException(
            f"{exc}. Start it with 'openbase-coder services start "
            f"{sync_daemon.SYNC_DAEMON_SERVICE_NAME}'."
        ) from None
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    click.echo(f"Role:      {payload.get('role') or '?'}")
    click.echo(f"Device:    {payload.get('device') or '?'}")
    roots = payload.get("roots") or []
    click.echo(f"Roots:     {len(roots)}")
    for root in roots:
        line = f"  {root.get('path') or root.get('id')}"
        details = [f"{int(root.get('entries') or 0)} entries"]
        if root.get("pending_fetches"):
            details.append(f"{root['pending_fetches']} transferring")
        if root.get("scanning"):
            details.append("scanning")
        click.echo(f"{line}  ({', '.join(details)})")
    peers = payload.get("peers") or []
    if peers:
        click.echo("Peers:")
        for peer in peers:
            rtt = peer.get("rtt_ms") or 0
            suffix = f", {rtt:.0f} ms" if rtt else ""
            click.echo(f"  {peer.get('device')} ({peer.get('role')}{suffix})")
    else:
        click.echo(
            click.style(
                "Peers:     none connected (is the other computer on and on "
                "Openbase VPN?)",
                fg="yellow",
            )
        )
    open_conflicts = int(payload.get("open_conflicts") or 0)
    if open_conflicts:
        click.echo(
            click.style(
                f"Conflicts: {open_conflicts} (see 'openbase-coder sync conflicts')",
                fg="yellow",
            )
        )
    else:
        click.echo("Conflicts: 0")


def _conflict_time(conflict: dict) -> str:
    created_ns = conflict.get("created_ns")
    if not created_ns:
        return ""
    try:
        moment = datetime.fromtimestamp(int(created_ns) / 1e9, tz=timezone.utc)
    except (OverflowError, ValueError, OSError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@sync.command()
@click.option("--json", "as_json", is_flag=True, help="Print the raw records.")
def conflicts(as_json: bool) -> None:
    """List open conflicts."""
    _require_configured()
    try:
        records = _client().conflicts()
    except sync_daemon.SyncDaemonError as exc:
        raise click.ClickException(str(exc)) from None
    if as_json:
        click.echo(json.dumps(records, indent=2, sort_keys=True))
        return
    if not records:
        click.echo("No open sync conflicts.")
        return
    for record in records:
        when = _conflict_time(record)
        click.echo(
            f"{record.get('id')}  {record.get('kind') or '?':<13} "
            f"{record.get('root') or '?'}:{record.get('path') or ''}"
            + (f"  ({when})" if when else "")
        )
    click.echo(
        "Resolve with 'openbase-coder sync resolve ID --keep-local' or '--use-remote'."
    )


@sync.command()
@click.argument("conflict_id", type=int)
@click.option(
    "--keep-local",
    "action",
    flag_value="keep_local",
    help="Keep this computer's version.",
)
@click.option(
    "--use-remote",
    "action",
    flag_value="use_remote",
    help="Take the other computer's version.",
)
def resolve(conflict_id: int, action: str | None) -> None:
    """Resolve one conflict by id."""
    if not action:
        raise click.ClickException("Pass --keep-local or --use-remote.")
    _require_configured()
    try:
        _client().resolve(conflict_id, "a" if action == "keep_local" else "b")
    except sync_daemon.SyncDaemonError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"Resolved conflict {conflict_id} with {action}.")


@sync.command("migrate-from-syncthing")
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    help="Make the changes. Without it, only print what would happen.",
)
@click.option(
    "--replace-nested",
    is_flag=True,
    help=(
        "When a migrated folder (e.g. ~/Projects) contains existing Openbase "
        "Sync roots, replace those inner roots with it instead of skipping it."
    ),
)
@click.option(
    "--remove-markers",
    is_flag=True,
    help=(
        "Also move the old engine's .stfolder/.stignore markers out of the "
        "synced folders. Only once the old sync is stopped on every computer."
    ),
)
@click.option(
    "--no-restart",
    is_flag=True,
    help="Do not restart the sync-daemon service after adding roots.",
)
def migrate_from_syncthing(
    apply_changes: bool, replace_nested: bool, remove_markers: bool, no_restart: bool
) -> None:
    """Move this computer from the previous (Syncthing-based) sync to Openbase Sync.

    Stops and uninstalls the old code-sync service, moves its engine state,
    version history and folder list into ~/.openbase/trash/, and turns its
    folders into Openbase Sync roots. Dry run unless --apply is passed; safe
    to run again and on computers that never used the old sync.
    """
    plan = sync_migration.plan_migration(
        replace_nested=replace_nested, include_markers=remove_markers
    )
    if plan.legacy_config.error:
        click.echo(click.style(f"  WARN  {plan.legacy_config.error}", fg="yellow"))
    _echo_plan(plan, verb="Will" if apply_changes else "Would", restart=not no_restart)
    if plan.nothing_to_do:
        click.echo("Nothing to migrate.")
        if not plan.daemon_configured and plan.roots:
            click.echo(sync_migration.configure_command_hint(plan.roots))
        return
    if not apply_changes:
        click.echo()
        click.echo("Dry run: nothing was changed. Re-run with --apply to migrate.")
        return

    result = sync_migration.apply_migration(plan, restart=not no_restart)
    click.echo()
    if result.service_removed:
        click.echo("Stopped and uninstalled the code-sync service.")
    for source, destination in result.moved:
        click.echo(f"Moved {source} -> {destination}")
    if result.roots_written:
        click.echo(f"Updated {sync_daemon.SYNC_DAEMON_CONFIG_PATH}.")
        if result.daemon_restarted:
            click.echo("Restarted the sync-daemon service.")
        elif not no_restart:
            click.echo(
                "The sync-daemon service is not installed; start it with "
                "'openbase-coder services start sync-daemon'."
            )
    if not plan.daemon_configured and plan.roots:
        click.echo()
        click.echo("Openbase Sync is not set up here yet. Configure it with:")
        click.echo("  " + sync_migration.configure_command_hint(plan.roots))
    click.echo("Migration complete.")


def _echo_plan(plan: sync_migration.MigrationPlan, *, verb: str, restart: bool) -> None:
    if plan.service_installed:
        click.echo(f"{verb} stop and uninstall the code-sync service.")
    for path in [*plan.trash_paths, *plan.markers]:
        click.echo(f"{verb} move {path} to {sync_migration.trash_dir()}/")
    if plan.roots:
        click.echo("Openbase Sync roots for the old folders:")
        for root in plan.roots:
            click.echo(f"  {root}")
    if plan.dropped_ignore_rules:
        click.echo(
            f"{plan.dropped_ignore_rules} custom ignore rule(s) of the old "
            "folders are not carried over (Openbase Sync skips dependency and "
            "build folders itself); they stay in the trashed sync-config.json."
        )
    if not plan.daemon_configured:
        return
    for root in plan.root_change.added:
        click.echo(f"{verb} add root {root['path']} (id {root['id']})")
    for root in plan.root_change.replaced:
        click.echo(f"{verb} replace nested root {root['path']}")
    for path, reason in plan.root_change.skipped:
        if reason.startswith("already inside"):
            continue
        click.echo(click.style(f"  SKIP  {path}: {reason}", fg="yellow"))
    if plan.root_change.changed and restart:
        click.echo(f"{verb} restart the sync-daemon service if it is installed.")
