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


@sync_daemon_cli.group("pair")
def pair_cli() -> None:
    """Pair this computer with your other computers (same as the Sync page).

    Run `pair hub` on the always-on computer, then `pair join <hub>` on each
    computer that should sync with it. The hub hands over its pair secret
    and folders over Openbase VPN; both computers must be signed in to the
    same Openbase account.
    """


def _pairing_call(func, *args, **kwargs):
    from openbase_coder_cli import sync_pairing

    try:
        return func(*args, **kwargs)
    except sync_pairing.PairingError as exc:
        raise click.ClickException(str(exc)) from None


@pair_cli.command("candidates")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def pair_candidates(as_json: bool) -> None:
    """List your other computers on Openbase VPN and their sync role."""
    from openbase_coder_cli import sync_pairing

    payload = sync_pairing.candidates()
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not payload["signed_in"]:
        click.echo("Not signed in: run 'openbase-coder login' to see your computers.")
    click.echo(f"This computer: {payload['role']}")
    if not payload["candidates"]:
        click.echo("No other computers found on Openbase VPN.")
        return
    for entry in payload["candidates"]:
        role = entry["role"] if entry["reachable"] else "offline"
        line = f"  {entry['name']:<28} {role:<8} {entry['host']}"
        if entry.get("hub_host"):
            line += f"  (hub: {entry.get('hub_name') or entry['hub_host']})"
        if entry.get("error"):
            line += f"  - {entry['error']}"
        click.echo(line)


@pair_cli.command("hub")
@click.option(
    "--root",
    "roots",
    multiple=True,
    help="Folder to sync (repeatable). Default: ~/Projects plus the Openbase folders.",
)
def pair_hub(roots: tuple[str, ...]) -> None:
    """Make this computer the hub (the always-on computer) and start syncing."""
    from openbase_coder_cli import sync_pairing

    result = _pairing_call(sync_pairing.become_hub, list(roots) or None)
    sync_pairing.refresh_cloud_registration(background=False)
    click.echo("This computer is now the hub. Folders:")
    for root in result["roots"]:
        click.echo(f"  {root['path']}")
    for skipped in result["skipped"]:
        click.echo(f"  skipped {skipped['path']}: {skipped['reason']}")
    click.echo(
        "On each other computer, run 'openbase-coder sync-daemon pair join "
        "<this computer>' or use its Sync page."
    )


@pair_cli.command("join")
@click.argument("hub")
@click.option(
    "--root",
    "roots",
    multiple=True,
    help="Only sync these of the hub's folders (repeatable). Default: all of them.",
)
def pair_join(hub: str, roots: tuple[str, ...]) -> None:
    """Sync this computer with HUB (its name or Openbase VPN address)."""
    from openbase_coder_cli import sync_pairing

    result = _pairing_call(sync_pairing.join_hub, hub, list(roots) or None)
    sync_pairing.refresh_cloud_registration(background=False)
    click.echo(f"Syncing with {result['hub_name']}. Folders:")
    for root in result["roots"]:
        click.echo(f"  {root['path']}")


@pair_cli.command("leave")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def pair_leave(yes: bool) -> None:
    """Stop syncing on this computer. Your files stay where they are."""
    from openbase_coder_cli import sync_pairing

    if not sync_daemon.is_configured():
        click.echo("Openbase Sync is not set up on this computer.")
        return
    if not yes:
        click.confirm("Stop syncing on this computer?", abort=True)
    result = _pairing_call(sync_pairing.leave)
    sync_pairing.refresh_cloud_registration(background=False)
    click.echo("Stopped syncing on this computer. Your files were not changed.")
    if result.get("config_moved_to"):
        click.echo(f"The old setup was moved to {result['config_moved_to']}.")


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


def _json_option(func):
    return click.option("--json", "as_json", is_flag=True, help="Print JSON.")(func)


def _apply_judgment(enabled: bool, *, restart: bool, as_json: bool) -> None:
    from openbase_coder_cli.services.cloud_registration import (
        CloudReportResult,
        local_device_id,
        register_and_report,
    )

    _require_configured()
    # The daemon sends this id as X-Openbase-Device-Id; the cloud allows the
    # call only for a registered device of the user with the opt-in set.
    device_id = local_device_id()
    try:
        path = sync_daemon.set_judgment(enabled, device_id)
    except sync_daemon.SyncDaemonError as exc:
        raise click.ClickException(str(exc)) from exc
    state = "enabled" if enabled else "disabled"
    if not as_json:
        click.echo(f"AI conflict labels {state} in {path}.")
        click.echo(f"Cloud device id: {device_id}")

    # The registration payload reads the opt-in from the config just written.
    try:
        report = register_and_report()
    except Exception as exc:  # noqa: BLE001 - registration is best-effort
        report = CloudReportResult(ok=False, supported=True, error=str(exc))
    if report.ok and not as_json:
        click.echo(f"Openbase Cloud updated (judgment_enabled={str(enabled).lower()}).")
    elif not report.ok and not as_json:
        click.echo(
            "Warning: could not update Openbase Cloud "
            f"({report.error or 'unknown error'}); it is retried at the next "
            "periodic device registration.",
            err=True,
        )

    restarted: bool | None = None
    if not restart:
        if not as_json:
            click.echo("Not restarting the sync-daemon service (--no-restart).")
        else:
            _print_judgment_result(enabled, device_id, path, report, restart, restarted)
        return
    try:
        restarted = sync_daemon.restart_service_if_installed()
    except Exception as exc:  # noqa: BLE001 - the setting is saved either way
        raise click.ClickException(
            f"Saved the setting, but restarting the sync-daemon service failed: "
            f"{exc}. Restart it with 'openbase-coder services restart sync-daemon'."
        ) from exc
    if restarted:
        if not as_json:
            click.echo("Restarted the sync-daemon service.")
    else:
        if not as_json:
            click.echo(
                "The sync-daemon service is not installed; the setting applies when "
                "it starts."
            )
    if as_json:
        _print_judgment_result(enabled, device_id, path, report, restart, restarted)


def _print_judgment_result(
    enabled: bool,
    device_id: str,
    path: Path,
    report,
    restart_requested: bool,
    restarted: bool | None,
) -> None:
    click.echo(
        json.dumps(
            {
                "configured": True,
                "enabled": enabled,
                "device_id": device_id,
                "config_path": str(path),
                "cloud": report.to_dict(),
                "restart": {
                    "requested": restart_requested,
                    "restarted": restarted,
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


_no_restart_option = click.option(
    "--no-restart",
    is_flag=True,
    help="Do not restart the sync-daemon service after changing the setting.",
)


@judgment_cli.command("enable")
@_no_restart_option
@_json_option
def judgment_enable(no_restart: bool, as_json: bool) -> None:
    """Opt this computer in: label sync conflicts with Openbase Cloud."""
    _apply_judgment(True, restart=not no_restart, as_json=as_json)


@judgment_cli.command("disable")
@_no_restart_option
@_json_option
def judgment_disable(no_restart: bool, as_json: bool) -> None:
    """Opt this computer out of AI conflict labels."""
    _apply_judgment(False, restart=not no_restart, as_json=as_json)


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
