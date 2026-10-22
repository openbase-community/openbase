"""``openbase-coder storage``: S3-compatible object storage reached through
Openbase. Keys live in Openbase Cloud's vault; this machine leases them into a
process that signs requests, so agents use ``http://127.0.0.1:<port>`` with
placeholder credentials and never see the real key."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import click

from openbase_coder_cli.connections import cloud, commands, state
from openbase_coder_cli.connections.s3_client import (
    S3RequestError,
    delete_object,
    get_object,
    list_objects,
    put_object,
)
from openbase_coder_cli.connections.s3_proxy import S3ProxyServer, S3Target
from openbase_coder_cli.connections.s3_signing import S3Credentials

KIND = cloud.KIND_S3
ACCESS_KEY_ENV_VAR = "OPENBASE_STORAGE_ACCESS_KEY_ID"
SECRET_KEY_ENV_VAR = "OPENBASE_STORAGE_SECRET_ACCESS_KEY"
READONLY_ACCESS_KEY_ENV_VAR = "OPENBASE_STORAGE_READONLY_ACCESS_KEY_ID"
READONLY_SECRET_KEY_ENV_VAR = "OPENBASE_STORAGE_READONLY_SECRET_ACCESS_KEY"
# Placeholder the agent's S3 client can be configured with; the proxy ignores
# the client's signature entirely.
PLACEHOLDER_ACCESS_KEY = "openbase"


def target_from_lease(leased: dict[str, Any]) -> S3Target:
    target = leased["target"]
    return S3Target(
        endpoint=str(target["endpoint"]).rstrip("/"),
        bucket=target["bucket"],
        region=target.get("region") or "auto",
        path_style=bool(target.get("path_style", True)),
    )


def credentials_from_lease(leased: dict[str, Any]) -> S3Credentials:
    credentials = leased["credentials"]
    return S3Credentials(
        access_key_id=credentials["access_key_id"],
        secret_access_key=credentials["secret_access_key"],
        session_token=credentials.get("session_token") or "",
    )


def _lease_for_role(
    name: str, read_only: bool, purpose: str
) -> tuple[dict, dict, bool]:
    """Lease the read-only key when asked and present; otherwise the
    read-write key, reporting whether the local proxy must enforce read-only."""
    connection = commands.resolve(KIND, name)
    roles = connection.get("roles") or []
    if read_only and cloud.ROLE_READONLY in roles:
        _connection, leased = commands.lease(KIND, name, cloud.ROLE_READONLY, purpose)
        return connection, leased, False
    _connection, leased = commands.lease(KIND, name, cloud.ROLE_READWRITE, purpose)
    return connection, leased, read_only


@click.group()
def storage() -> None:
    """Use S3-compatible object storage through Openbase without seeing the keys."""


@storage.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def list_command(as_json: bool) -> None:
    """List the buckets in this account's vault and any local proxies."""
    try:
        connections = cloud.list_connections(KIND)
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    commands.print_connections(KIND, connections, as_json=as_json)


@storage.command()
@click.argument("name")
@click.option(
    "--endpoint",
    required=True,
    help="S3 endpoint URL, e.g. https://s3.us-east-005.backblazeb2.com",
)
@click.option("--bucket", required=True, help="Bucket this connection is limited to.")
@click.option(
    "--region",
    default="auto",
    show_default=True,
    help="SigV4 region (auto for R2/Tigris).",
)
@click.option(
    "--virtual-host",
    is_flag=True,
    help="Address the bucket as a subdomain of the endpoint.",
)
@click.option(
    "--access-key-id", help=f"Read-write key id (or set {ACCESS_KEY_ENV_VAR})."
)
@click.option(
    "--secret-access-key", help=f"Read-write secret (or set {SECRET_KEY_ENV_VAR})."
)
@click.option(
    "--readonly-access-key-id",
    help=f"Optional read-only key id (or set {READONLY_ACCESS_KEY_ENV_VAR}).",
)
@click.option(
    "--readonly-secret-access-key",
    help=f"Optional read-only secret (or set {READONLY_SECRET_KEY_ENV_VAR}).",
)
def add(
    name: str,
    endpoint: str,
    bucket: str,
    region: str,
    virtual_host: bool,
    access_key_id: str | None,
    secret_access_key: str | None,
    readonly_access_key_id: str | None,
    readonly_secret_access_key: str | None,
) -> None:
    """Store an existing bucket's endpoint and keys in the Openbase Cloud vault as NAME."""
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise click.ClickException("The endpoint must be an http(s) URL.")
    target = {
        "endpoint": endpoint.rstrip("/"),
        "bucket": bucket,
        "region": region,
        "path_style": not virtual_host,
    }
    credentials = {
        cloud.ROLE_READWRITE: {
            "access_key_id": commands.read_secret_option(
                access_key_id, ACCESS_KEY_ENV_VAR, "Access key id"
            ),
            "secret_access_key": commands.read_secret_option(
                secret_access_key, SECRET_KEY_ENV_VAR, "Secret access key"
            ),
        }
    }
    readonly_access_key_id = readonly_access_key_id or os.environ.get(
        READONLY_ACCESS_KEY_ENV_VAR, ""
    )
    if readonly_access_key_id:
        credentials[cloud.ROLE_READONLY] = {
            "access_key_id": readonly_access_key_id,
            "secret_access_key": commands.read_secret_option(
                readonly_secret_access_key,
                READONLY_SECRET_KEY_ENV_VAR,
                "Read-only secret access key",
            ),
        }
    try:
        created = cloud.add_connection(KIND, name, target, credentials)
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    state.audit(KIND, name, "added", f"{endpoint} {bucket}")
    click.echo(
        f"Added storage '{name}' ({endpoint}, bucket {bucket}, roles: "
        f"{', '.join(created.get('roles') or [])})."
    )
    click.echo(f"  Use it: openbase-coder storage proxy {name} --port 9000")


@storage.command()
@click.argument("name")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def remove(name: str, yes: bool) -> None:
    """Forget NAME (the bucket and its keys are left untouched at the provider)."""
    connection = commands.resolve(KIND, name)
    commands.confirm_or_yes(
        f"Remove '{name}' from the vault (the bucket itself is kept)?", yes
    )
    state.stop_proxy(KIND, name)
    try:
        cloud.remove_connection(connection["id"])
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    state.audit(KIND, name, "removed")
    click.echo(f"Removed '{name}'.")


@storage.command()
@click.argument("name")
def revoke(name: str) -> None:
    """Block all further leases of NAME; running proxies close within a minute."""
    connection = commands.resolve(KIND, name)
    try:
        cloud.revoke_connection(connection["id"])
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    state.stop_proxy(KIND, name)
    state.audit(KIND, name, "revoked")
    click.echo(
        f"Revoked access to '{name}'. Restore with 'openbase-coder storage restore {name}'."
    )


@storage.command()
@click.argument("name")
def restore(name: str) -> None:
    """Allow leases of NAME again after a revoke."""
    connection = commands.resolve(KIND, name)
    try:
        cloud.restore_connection(connection["id"])
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    state.audit(KIND, name, "restored")
    click.echo(f"Restored access to '{name}'.")


@storage.command()
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def audit(name: str, as_json: bool) -> None:
    """Show who leased, changed or revoked NAME (from Openbase Cloud)."""
    connection = commands.resolve(KIND, name)
    try:
        events = cloud.connection_events(connection["id"])
    except cloud.ConnectionsError as exc:
        raise click.ClickException(str(exc)) from exc
    commands.print_events(events, as_json=as_json)


@storage.command()
@click.argument("name")
@click.option(
    "--port",
    type=click.IntRange(0, 65535),
    default=0,
    help="Loopback port (0 = any free port).",
)
@click.option(
    "--read-only",
    is_flag=True,
    help="Only allow GET/HEAD (uses the read-only key when one exists).",
)
@click.option("--purpose", default="storage proxy", help="Recorded in the audit log.")
@click.option(
    "--background", is_flag=True, help="Detach; stop later with 'storage stop'."
)
def proxy(
    name: str, port: int, read_only: bool, purpose: str, background: bool
) -> None:
    """Serve NAME's bucket on http://127.0.0.1:PORT for any S3 client (path-style)."""
    if background:
        argv = ["storage", "proxy", name, "--port", str(port), "--purpose", purpose]
        if read_only:
            argv.append("--read-only")
        running = commands.spawn_background_proxy(KIND, name, argv)
        click.echo(
            f"Proxying storage '{name}' ({running.role}) at {running.url} (pid {running.pid})"
        )
        click.echo(f"  Stop it: openbase-coder storage stop {name}")
        return
    _connection, leased, enforce_read_only = _lease_for_role(name, read_only, purpose)
    role = leased["role"] + ("+local-read-only" if enforce_read_only else "")
    connection_id = leased["id"]
    target = target_from_lease(leased)

    def start(log):
        try:
            server = S3ProxyServer(
                port,
                target,
                credentials_from_lease(leased),
                read_only=read_only,
                log=log,
                still_allowed=lambda: cloud.connection_allowed(connection_id),
            )
        except OSError as exc:
            raise click.ClickException(
                f"Cannot listen on 127.0.0.1:{port}: {exc}"
            ) from exc
        server.start()
        click.echo(
            f"  Clients: endpoint {server.local_url}, path-style, bucket {target.bucket}, "
            f"any access key (e.g. {PLACEHOLDER_ACCESS_KEY}/{PLACEHOLDER_ACCESS_KEY})."
        )
        if enforce_read_only:
            click.echo(
                "  No read-only key is stored; this proxy refuses writes locally."
            )
        return server

    commands.run_proxy_until_stopped(KIND, name, role, start)


@storage.command()
@click.argument("name")
def stop(name: str) -> None:
    """Stop the background proxy for NAME."""
    commands.stop_proxy_command(KIND, name)


def _client(name: str, read_only: bool, purpose: str) -> tuple[S3Target, S3Credentials]:
    _connection, leased, _enforce = _lease_for_role(name, read_only, purpose)
    return target_from_lease(leased), credentials_from_lease(leased)


@storage.command("ls")
@click.argument("name")
@click.argument("prefix", default="")
@click.option(
    "--limit", type=click.IntRange(1, 100000), default=1000, show_default=True
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def ls(name: str, prefix: str, limit: int, as_json: bool) -> None:
    """List objects in NAME's bucket, optionally under PREFIX."""
    target, credentials = _client(name, True, "storage ls")
    try:
        objects = list(list_objects(target, credentials, prefix, limit=limit))
    except (S3RequestError, OSError) as exc:
        raise click.ClickException(f"Listing failed: {exc}") from exc
    if as_json:
        click.echo(json.dumps([item.__dict__ for item in objects], indent=2))
        return
    if not objects:
        click.echo("No objects.")
        return
    for item in objects:
        click.echo(f"{item.last_modified:<25} {item.size:>12}  {item.key}")


@storage.command("get")
@click.argument("name")
@click.argument("key")
@click.argument("destination", required=False, type=click.Path(dir_okay=True))
def get(name: str, key: str, destination: str | None) -> None:
    """Download KEY from NAME's bucket to DESTINATION (default: the key's basename)."""
    path = Path(destination) if destination else Path(Path(key).name or "object")
    if path.is_dir():
        path = path / (Path(key).name or "object")
    target, credentials = _client(name, True, "storage get")
    try:
        size = get_object(target, credentials, key, path)
    except (S3RequestError, OSError) as exc:
        raise click.ClickException(f"Download failed: {exc}") from exc
    click.echo(f"Downloaded {key} ({size} bytes) to {path}")


@storage.command("put")
@click.argument("name")
@click.argument("source", type=click.Path(exists=True, dir_okay=False))
@click.argument("key", required=False)
@click.option(
    "--content-type", default="", help="Content-Type to store with the object."
)
def put(name: str, source: str, key: str | None, content_type: str) -> None:
    """Upload SOURCE to NAME's bucket as KEY (default: the file name)."""
    path = Path(source)
    key = key or path.name
    target, credentials = _client(name, False, "storage put")
    try:
        put_object(target, credentials, key, path, content_type=content_type)
    except (S3RequestError, OSError) as exc:
        raise click.ClickException(f"Upload failed: {exc}") from exc
    click.echo(f"Uploaded {path} as {key} ({path.stat().st_size} bytes)")


@storage.command("rm")
@click.argument("name")
@click.argument("key")
def rm(name: str, key: str) -> None:
    """Delete KEY from NAME's bucket."""
    target, credentials = _client(name, False, "storage rm")
    try:
        delete_object(target, credentials, key)
    except (S3RequestError, OSError) as exc:
        raise click.ClickException(f"Delete failed: {exc}") from exc
    click.echo(f"Deleted {key}")
