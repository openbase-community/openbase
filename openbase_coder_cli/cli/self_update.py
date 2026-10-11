from __future__ import annotations

import json

import click

from openbase_coder_cli.self_update import (
    SelfUpdateError,
    check_for_update,
    result_payload,
    run_automatic_self_update,
    run_self_update,
)


@click.command("self-update")
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help="Only check for an available update; do not install.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Update even while a voice session is active.",
)
@click.option(
    "--automatic",
    is_flag=True,
    hidden=True,
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the result as JSON (for UI-driven updates).",
)
def self_update(check_only: bool, force: bool, automatic: bool, as_json: bool) -> None:
    """Update a standalone install to the latest release for its channel.

    The update sequence, rollback behavior, and channel semantics are
    documented in the workspace AUTO_UPDATE.md guide.
    """
    try:
        if check_only:
            check = check_for_update()
            if as_json:
                click.echo(json.dumps(check.__dict__))
            elif check.update_available:
                click.echo(
                    f"Update available: {check.current_version} -> "
                    f"{check.latest_version} ({check.channel})"
                )
            else:
                click.echo(
                    check.detail
                    or f"Up to date ({check.current_version}, {check.channel})."
                )
            return

        updater = run_automatic_self_update if automatic else run_self_update
        result = updater(force=force, report=click.echo)
    except SelfUpdateError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(json.dumps(result_payload(result)))
        return
    if result.status == "updated":
        click.echo(f"Updated {result.from_version} -> {result.to_version}.")
    elif result.status == "up-to-date":
        click.echo(f"Already up to date ({result.from_version}).")
    else:
        click.echo(f"{result.status}: {result.detail}")
    if result.status in ("rolled-back", "blocked"):
        raise SystemExit(1)


@click.command("version")
@click.option("--json", "as_json", is_flag=True, help="Emit version facts as JSON.")
def version_command(as_json: bool) -> None:
    """Show version facts: CLI, package, channel, and update status."""
    from openbase_coder_cli.self_update import version_info

    info = version_info()
    if as_json:
        click.echo(json.dumps(info, indent=2, sort_keys=True))
        return
    click.echo(f"openbase-coder {info['cli']}")
    if info["standalone"]:
        click.echo(
            f"standalone package {info.get('package_version', '?')} "
            f"({info.get('target', '?')}, {info['channel']} channel, "
            f"layout {info['layout_version']})"
        )
    else:
        click.echo("development workspace install (git-managed)")
    if info.get("update_required"):
        click.echo(f"UPDATE REQUIRED: {info.get('latest_version', 'a newer version')}")
    elif info.get("update_available"):
        click.echo(f"update available: {info.get('latest_version')}")
