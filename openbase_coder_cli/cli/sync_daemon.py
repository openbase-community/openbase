"""``openbase-coder sync-daemon`` — the hub/edge mirror (Openbase Sync)."""

from __future__ import annotations

import json
from pathlib import Path

import click

from openbase_coder_cli import sync_daemon, sync_state
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
    default=None,
    type=int,
    help=(
        "Never write below this much free disk (MB). Default: derived from "
        "the disk size, min(10 GiB, 10%)."
    ),
)
@click.option(
    "--project-only",
    is_flag=True,
    help=(
        "Keep large files as placeholders on this computer until they are "
        "used, whatever --anchor says (a small cloud workspace)."
    ),
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
    low_water_mb: int | None,
    project_only: bool,
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
        thin=True if project_only else None,
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


def _echo_restart_required(result: dict) -> None:
    if result.get("restart_required"):
        click.echo("Restart Openbase to finish.")


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
    _echo_restart_required(result)


def _size(value) -> str:
    from openbase_coder_cli.cli.sync import _size as size

    return size(value) if value is not None else "?"


@pair_cli.command("folders")
@click.argument("hub")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def pair_folders(hub: str, as_json: bool) -> None:
    """Show HUB's folders, their size and this computer's free disk.

    Use it before `pair join` to choose which folders to sync here.
    """
    from openbase_coder_cli import sync_pairing

    preview = _pairing_call(sync_pairing.hub_folders, hub)
    if as_json:
        click.echo(json.dumps(preview, indent=2, sort_keys=True))
        return
    disk = preview["this_computer"]["disk"]
    click.echo(
        f"{preview['hub_name']} syncs these folders "
        f"(this computer: {_size(disk.get('free_bytes'))} free of "
        f"{_size(disk.get('total_bytes'))}):"
    )
    for folder in preview["folders"]:
        files = folder.get("files")
        size = (
            f"{files} files, {_size(folder.get('bytes'))}"
            if files is not None
            else "size unknown"
        )
        click.echo(f"  {folder['path']:<40} {size}")
        for sub in folder.get("subfolders") or []:
            sub_size = (
                f"{sub['files']} files, {_size(sub.get('bytes'))}"
                if sub.get("files") is not None
                else "size unknown"
            )
            click.echo(f"    {sub['path']:<38} {sub_size}")
    if preview["project_only"]:
        click.echo(
            "This is a cloud workspace: choose the projects it syncs (a folder "
            "above, or a project inside one), e.g. 'openbase-coder sync-daemon "
            f"pair join {hub} --root <folder>'."
        )


@pair_cli.command("join")
@click.argument("hub")
@click.option(
    "--root",
    "roots",
    multiple=True,
    help=(
        "Only sync these of the hub's folders, or projects inside one, such "
        "as ~/Projects/app (repeatable). Default: all of them; on a cloud "
        "workspace at least one is required."
    ),
)
@click.option(
    "--project-only/--full-copy",
    "project_only",
    default=None,
    help=(
        "Project-only: sync just the chosen folders and keep large files on "
        "the hub until used. Default: on for a cloud workspace, off elsewhere."
    ),
)
def pair_join(hub: str, roots: tuple[str, ...], project_only: bool | None) -> None:
    """Sync this computer with HUB (its name or Openbase VPN address)."""
    from openbase_coder_cli import sync_pairing

    result = _pairing_call(
        sync_pairing.join_hub, hub, list(roots) or None, project_only=project_only
    )
    sync_pairing.refresh_cloud_registration(background=False)
    mode = " (project-only)" if result.get("project_only") else ""
    click.echo(f"Syncing with {result['hub_name']}{mode}. Folders:")
    for root in result["roots"]:
        click.echo(f"  {root['path']}")
    for warning in result.get("warnings") or []:
        click.echo(click.style(warning, fg="yellow"))
    _echo_restart_required(result)


@pair_cli.command("add-folder")
@click.argument("path")
def pair_add_folder(path: str) -> None:
    """Sync PATH too. On a computer that syncs some of the hub's folders,
    PATH may be one of the others: it is created here and fills from the hub."""
    from openbase_coder_cli import sync_pairing

    result = _pairing_call(sync_pairing.add_root, path)
    click.echo(f"Now syncing {result['root']['path']}.")
    for peer in result.get("peers") or []:
        if not peer["ok"]:
            click.echo(click.style(f"  {peer['name']}: {peer['error']}", fg="yellow"))
    _echo_restart_required(result)


@pair_cli.command("remove-folder")
@click.argument("path")
@click.option(
    "--this-computer",
    "scope",
    flag_value="this_computer",
    default=None,
    help="Stop syncing PATH here only; the hub and other computers keep it.",
)
@click.option(
    "--everywhere",
    "scope",
    flag_value="everywhere",
    help="Stop syncing PATH on every computer.",
)
def pair_remove_folder(path: str, scope: str | None) -> None:
    """Stop syncing PATH. Files stay on disk.

    Default: here only on a project-only computer, everywhere otherwise.
    """
    from openbase_coder_cli import sync_pairing

    result = _pairing_call(sync_pairing.remove_root, path, scope=scope)
    where = "on this computer" if result.get("scope") == "this_computer" else "anywhere"
    click.echo(f"Stopped syncing {path} {where}. Files were not changed.")
    _echo_restart_required(result)


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
    _echo_restart_required(result)


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
    client = sync_daemon.SyncDaemonClient()
    try:
        conflicts = client.conflicts()
        refusal = sync_state.branch_refusal(conflicts, conflict_id)
    except sync_daemon.SyncDaemonError as exc:
        raise click.ClickException(str(exc)) from None
    if refusal:
        raise click.ClickException(refusal)
    conflict = sync_state.find_conflict(conflicts, conflict_id)
    if conflict is None:
        raise click.ClickException("This conflict is no longer open.")
    local_device = str(sync_daemon.read_config_summary().get("device_id") or "")
    choice = sync_state.resolution_choice(conflict, action, local_device)
    try:
        client.resolve(conflict_id, choice)
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


@sync_daemon_cli.group("agent-config")
def agent_config_cli() -> None:
    """Sync the coding agents' skills, MCP servers and sign-in (on by default).

    Claude Code's and Codex's user-level MCP servers, linked skills and
    sign-in material travel between your computers through Openbase Sync.
    Sign-in material never appears in previous versions, conflict copies or
    logs. Disable it on a computer that should keep its own agent setup.
    """


def _apply_agent_config(enabled: bool, *, restart: bool, as_json: bool) -> None:
    _require_configured()
    try:
        path = sync_daemon.set_agent_config(enabled)
    except sync_daemon.SyncDaemonError as exc:
        raise click.ClickException(str(exc)) from exc
    restarted: bool | None = None
    if restart:
        try:
            restarted = sync_daemon.restart_service_if_installed()
        except Exception as exc:  # noqa: BLE001 - the setting is saved either way
            raise click.ClickException(
                f"Saved the setting, but restarting the sync-daemon service failed: "
                f"{exc}. Restart it with 'openbase-coder services restart sync-daemon'."
            ) from exc
    if as_json:
        click.echo(
            json.dumps(
                {
                    "configured": True,
                    "enabled": enabled,
                    "config_path": str(path),
                    "restart": {"requested": restart, "restarted": restarted},
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    state = "on" if enabled else "off"
    click.echo(f"Agent configuration sync {state} in {path}.")
    if not restart:
        click.echo("Not restarting the sync-daemon service (--no-restart).")
    elif restarted:
        click.echo("Restarted the sync-daemon service.")
    else:
        click.echo(
            "The sync-daemon service is not installed; the setting applies when it starts."
        )


@agent_config_cli.command("enable")
@_no_restart_option
@_json_option
def agent_config_enable(no_restart: bool, as_json: bool) -> None:
    """Sync skills, MCP servers and sign-in on this computer (the default)."""
    _apply_agent_config(True, restart=not no_restart, as_json=as_json)


@agent_config_cli.command("disable")
@_no_restart_option
@_json_option
def agent_config_disable(no_restart: bool, as_json: bool) -> None:
    """Keep this computer's agent setup local."""
    _apply_agent_config(False, restart=not no_restart, as_json=as_json)


@agent_config_cli.command("status")
@click.option("--json", "as_json", is_flag=True, help="Print the status as JSON.")
def agent_config_status(as_json: bool) -> None:
    """Show whether agent configuration sync is on for this computer."""
    enabled = sync_daemon.agent_config_enabled()
    payload = {"configured": enabled is not None, "enabled": bool(enabled)}
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if enabled is None:
        click.echo("Openbase Sync is not set up on this computer.")
        return
    click.echo(
        "Agent configuration sync is on: skills, MCP servers and sign-in of Claude "
        "Code and Codex follow you to your other computers."
        if enabled
        else "Agent configuration sync is off on this computer "
        "(openbase-coder sync-daemon agent-config enable)."
    )
