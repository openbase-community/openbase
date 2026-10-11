"""`openbase-coder ports listening`: which local servers are up right now.

A login that redirects to localhost starts a callback server; seeing its port
appear tells an agent which port the phone has to forward.
"""

from __future__ import annotations

import json
import os

import click
import psutil

# Openbase's own fixed services, hidden unless --all.
OPENBASE_PORTS = {7880, 7881, 7998, 7999, 18080, 59443}


def listening_ports(*, include_openbase: bool = False) -> list[dict]:
    rows: dict[int, dict] = {}
    for process in psutil.process_iter(["pid", "name", "uids", "cmdline"]):
        try:
            if process.info["uids"] and process.info["uids"].real != os.getuid():
                continue
            connections = process.net_connections(kind="tcp")
        except (
            psutil.AccessDenied,
            psutil.NoSuchProcess,
            psutil.ZombieProcess,
            RuntimeError,  # macOS: a process that exits mid-scan
        ):
            continue
        for conn in connections:
            if conn.status != psutil.CONN_LISTEN or not conn.laddr:
                continue
            port = conn.laddr.port
            if port in OPENBASE_PORTS and not include_openbase:
                continue
            row = rows.setdefault(
                port,
                {
                    "port": port,
                    "addresses": [],
                    "pid": process.info["pid"],
                    "command": " ".join(
                        (process.info["cmdline"] or [process.info["name"] or ""])[:4]
                    )[:120],
                },
            )
            if conn.laddr.ip not in row["addresses"]:
                row["addresses"].append(conn.laddr.ip)
    return sorted(rows.values(), key=lambda row: row["port"])


@click.group()
def ports() -> None:
    """Inspect local TCP ports."""


@ports.command("listening")
@click.option(
    "--all", "include_openbase", is_flag=True, help="Include Openbase's own services."
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def listening_command(include_openbase: bool, as_json: bool) -> None:
    """List TCP ports your processes are listening on."""
    rows = listening_ports(include_openbase=include_openbase)
    if as_json:
        click.echo(json.dumps(rows))
        return
    if not rows:
        click.echo("No listening ports.")
    for row in rows:
        click.echo(
            f"{row['port']:<6} {','.join(row['addresses']):<22} pid {row['pid']:<7} {row['command']}"
        )
