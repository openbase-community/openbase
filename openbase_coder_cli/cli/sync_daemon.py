"""``openbase-coder sync-daemon`` — the hub/edge mirror (Openbase Sync)."""

from __future__ import annotations

import json
from pathlib import Path

import click

from openbase_coder_cli import sync_daemon
from openbase_coder_cli.paths import OPENBASE_BIN_DIR


@click.group("sync-daemon")
def sync_daemon_cli() -> None:
    """Openbase Sync: mirror your projects between this computer and your hub."""


@sync_daemon_cli.command("configure")
@click.option("--role", type=click.Choice(["hub", "edge"]), required=True)
@click.option(
    "--peer", "peer", default="", help="Hub address (host or IP) for an edge."
)
@click.option(
    "--listen", "listen", default="", help="Hub bind address (tailnet IP) for a hub."
)
@click.option(
    "--root",
    "roots",
    multiple=True,
    help="Directory to mirror, absolute or ~/... (repeatable).",
)
@click.option(
    "--with-product-folders",
    is_flag=True,
    help=(
        "Also mirror the Openbase folders other features exchange through "
        "sync: the thread exchange (~/.openbase/thread-sync), personal skills "
        "(~/.agents/skills) and the folders those skills link to."
    ),
)
@click.option(
    "--pair-secret",
    default="",
    help="Shared secret; generated for a hub, required for an edge.",
)
@click.option("--group", default="default", help="Sync group name.")
@click.option(
    "--low-water-mb",
    default=10240,
    type=int,
    help="Never write below this much free disk (MB).",
)
@click.option(
    "--anchor",
    type=click.Choice(["hub", "edge"]),
    default="hub",
    show_default=True,
    help=(
        "Which side keeps every file in full. The other side keeps stubs for "
        "large files until they are used. Choose edge when the hub has less disk."
    ),
)
@click.option(
    "--start/--no-start",
    default=True,
    help="Install and start the service after writing config.",
)
def configure(
    role: str,
    peer: str,
    listen: str,
    roots: tuple[str, ...],
    with_product_folders: bool,
    pair_secret: str,
    group: str,
    low_water_mb: int,
    anchor: str,
    start: bool,
) -> None:
    """Write the daemon config and (by default) install and start the service."""
    if role == "edge" and not peer:
        raise click.UsageError("--peer is required for an edge")
    if role == "edge" and not pair_secret:
        raise click.UsageError(
            "--pair-secret is required for an edge (copy it from the hub's configure output)"
        )
    if role == "hub" and not listen:
        raise click.UsageError(
            "--listen is required for a hub (its Openbase VPN address)"
        )
    root_paths = list(roots)
    if with_product_folders:
        root_paths += sync_daemon.product_folder_roots()
    if not root_paths:
        raise click.UsageError(
            "at least one --root (or --with-product-folders) is required"
        )
    root_entries, change = sync_daemon.plan_root_additions([], root_paths)
    for path, reason in change.skipped:
        click.echo(f"Skipping root {path}: {reason}")
    secret = pair_secret or sync_daemon.new_pair_secret()
    config = sync_daemon.SyncDaemonConfig(
        device_id=sync_daemon.default_device_id(),
        sync_group=group,
        role=role,
        pair_secret=secret,
        roots=root_entries,
        listen_hot=f"{listen}:{sync_daemon.DEFAULT_HOT_PORT}",
        listen_bulk=f"{listen}:{sync_daemon.DEFAULT_BULK_PORT}",
        peer_hot=f"{peer}:{sync_daemon.DEFAULT_HOT_PORT}",
        peer_bulk=f"{peer}:{sync_daemon.DEFAULT_BULK_PORT}",
        low_water_mb=low_water_mb,
        anchor=anchor,
    )
    path = sync_daemon.write_config(config)
    click.echo(f"Wrote {path}")
    if role == "hub":
        click.echo(f"Pair secret (use on the edge): {secret}")
    if start:
        from openbase_coder_cli.services.installation import InstallationConfig
        from openbase_coder_cli.services.launchd import install_service
        from openbase_coder_cli.services.registry import find_service

        install_service(
            InstallationConfig.load(),
            find_service(sync_daemon.SYNC_DAEMON_SERVICE_NAME),
        )
        click.echo("Service sync-daemon installed and started.")


@sync_daemon_cli.command("install-binary")
@click.argument("source", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--ctl",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Also install the openbase-sync control binary.",
)
@click.option(
    "--edge",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Also install the edge relay binary (runs display-bound commands on the laptop).",
)
def install_binary(source: str, ctl: str | None, edge: str | None) -> None:
    """Copy a built openbase-syncd (and optionally openbase-sync, edge) into ~/.openbase/bin."""
    OPENBASE_BIN_DIR.mkdir(parents=True, exist_ok=True)
    pairs = [(source, sync_daemon.SYNC_DAEMON_BINARY_NAME)]
    if ctl:
        pairs.append((ctl, sync_daemon.SYNC_CTL_BINARY_NAME))
    if edge:
        pairs.append((edge, sync_daemon.SYNC_EDGE_BINARY_NAME))
    for src, name in pairs:
        dest = sync_daemon.install_executable(Path(src), OPENBASE_BIN_DIR / name)
        click.echo(f"Installed {dest}")


@sync_daemon_cli.command("status")
def status_cmd() -> None:
    """Daemon status (peers, roots, open conflicts)."""
    try:
        payload = sync_daemon.SyncDaemonClient().status()
    except sync_daemon.SyncDaemonError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(payload, indent=2))


@sync_daemon_cli.command("conflicts")
def conflicts_cmd() -> None:
    """List open conflicts."""
    try:
        conflicts = sync_daemon.SyncDaemonClient().conflicts()
    except sync_daemon.SyncDaemonError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(conflicts, indent=2))


@sync_daemon_cli.command("resolve")
@click.argument("conflict_id", type=int)
@click.argument("action", type=click.Choice(["keep_local", "use_remote"]))
def resolve_cmd(conflict_id: int, action: str) -> None:
    """Resolve a conflict by keeping this computer's version or taking the other's."""
    try:
        sync_daemon.SyncDaemonClient().resolve(
            conflict_id, "a" if action == "keep_local" else "b"
        )
    except sync_daemon.SyncDaemonError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("resolved")


@sync_daemon_cli.command("install-hooks")
def install_hooks_cmd() -> None:
    """Install the Claude Code hook scripts and shell snippet via openbase-sync."""
    import subprocess

    ctl = OPENBASE_BIN_DIR / sync_daemon.SYNC_CTL_BINARY_NAME
    if not ctl.is_file():
        raise click.ClickException(
            f"{ctl} not found; run `openbase-coder sync-daemon install-binary ... --ctl ...` first"
        )
    result = subprocess.run(
        [str(ctl), "install-hooks"], check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise click.ClickException(result.stderr.strip() or "install-hooks failed")
    click.echo(result.stdout.strip())


@sync_daemon_cli.command("disable")
def disable_cmd() -> None:
    """Stop and remove the daemon service; the config and state are kept."""
    from openbase_coder_cli.services.launchd import remove_service
    from openbase_coder_cli.services.registry import find_service

    remove_service(find_service(sync_daemon.SYNC_DAEMON_SERVICE_NAME))
    click.echo("Service sync-daemon removed.")


@sync_daemon_cli.group("judgment")
def judgment_cli() -> None:
    """AI conflict labels from Openbase Cloud (opt-in per computer).

    When enabled, the daemon sends both versions of a conflicting text file
    to Openbase Cloud, which labels the conflict. Conflicts are never
    resolved automatically.
    """


def _require_configured() -> None:
    if not sync_daemon.is_configured():
        raise click.ClickException(
            "Openbase Sync is not set up on this computer; run "
            "'openbase-coder sync-daemon configure' first."
        )


def _apply_judgment(enabled: bool, *, restart: bool) -> None:
    from openbase_coder_cli.services.cloud_registration import (
        local_device_id,
        register_and_report,
    )

    _require_configured()
    # The daemon sends this id as X-Openbase-Device-Id; the cloud allows the
    # call only for a registered device of the user with the opt-in set.
    device_id = local_device_id() if enabled else None
    try:
        path = sync_daemon.set_judgment(enabled, device_id)
    except sync_daemon.SyncDaemonError as exc:
        raise click.ClickException(str(exc)) from exc
    state = "enabled" if enabled else "disabled"
    click.echo(f"AI conflict labels {state} in {path}.")
    if device_id:
        click.echo(f"Cloud device id: {device_id}")

    # The registration payload reads the opt-in from the config just written.
    report = register_and_report()
    if report.ok:
        click.echo(f"Openbase Cloud updated (judgment_enabled={str(enabled).lower()}).")
    else:
        click.echo(
            "Warning: could not update Openbase Cloud "
            f"({report.error or 'unknown error'}); it is retried at the next "
            "periodic device registration.",
            err=True,
        )

    if not restart:
        click.echo("Not restarting the sync-daemon service (--no-restart).")
        return
    try:
        restarted = sync_daemon.restart_service_if_installed()
    except Exception as exc:  # noqa: BLE001 - the setting is saved either way
        raise click.ClickException(
            f"Saved the setting, but restarting the sync-daemon service failed: "
            f"{exc}. Restart it with 'openbase-coder services restart sync-daemon'."
        ) from exc
    if restarted:
        click.echo("Restarted the sync-daemon service.")
    else:
        click.echo(
            "The sync-daemon service is not installed; the setting applies when "
            "it starts."
        )


_no_restart_option = click.option(
    "--no-restart",
    is_flag=True,
    help="Do not restart the sync-daemon service after changing the setting.",
)


@judgment_cli.command("enable")
@_no_restart_option
def judgment_enable(no_restart: bool) -> None:
    """Opt this computer in: label sync conflicts with Openbase Cloud."""
    _apply_judgment(True, restart=not no_restart)


@judgment_cli.command("disable")
@_no_restart_option
def judgment_disable(no_restart: bool) -> None:
    """Opt this computer out of AI conflict labels."""
    _apply_judgment(False, restart=not no_restart)


@judgment_cli.command("status")
@click.option("--json", "as_json", is_flag=True, help="Print the status as JSON.")
def judgment_status(as_json: bool) -> None:
    """Show whether AI conflict labels are enabled and which device id is used."""
    configured = sync_daemon.is_configured()
    settings = sync_daemon.judgment_settings() if configured else None
    payload = {
        "configured": configured,
        "enabled": bool(settings and settings["enabled"]),
        "device_id": (settings or {}).get("device_id") or None,
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not configured:
        click.echo("Openbase Sync is not set up on this computer.")
        return
    click.echo(f"AI conflict labels: {'enabled' if payload['enabled'] else 'disabled'}")
    if payload["device_id"]:
        click.echo(f"Cloud device id:    {payload['device_id']}")
